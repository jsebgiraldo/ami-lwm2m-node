#!/usr/bin/env python3
"""fnb_power_logger.py -- continuous power/consumption logger for the FNIRSI FNB-C2.

WHY THIS EXISTS
---------------
We are standing up a LOCAL benchtop test network to hunt firmware leaks/bugs in the
AMI LwM2M node under REAL power monitoring. A single XIAO ESP32-C6 draws ~120-160 mA
idle on 5 V; the chronic "brownout" that plagued the fleet turned out to be a
USB-host over-current cliff triggered by LwM2M/802.15.4 TX bursts (see MEMORY.md:
"LwM2M burst = USB cliff"). To catch a repeat of that on the bench we need a
sample-accurate, always-on current/voltage trace with peak tracking -- exactly what
the FNIRSI FNB-C2 inline USB power meter provides at ~100 Hz.

This logger:
  * opens the FNB-C2 HID data interface (VID 0x2E3C / PID 0x5558, interface MI_02),
  * sends the documented init handshake so the meter starts streaming,
  * decodes bus voltage (V) + current (A) at 100 Hz (4 samples per 64-byte report),
  * integrates mAh and Wh, tracks peak current,
  * appends every sample to a timestamped CSV under tools/,
  * maintains a rolling tools/fnb_latest.json shaped like the canonical lab-snapshot
    "power" block so tools/lab_e2e_snapshot builder + the dashboard artifact can read
    a single agreed shape,
  * prints a compact live line each --interval seconds,
  * reconnects on device drop and never crashes the loop.

It is deliberately source-agnostic: if the meter is unplugged the loop logs and keeps
retrying; a dead source degrades numbers to the last known / zero rather than crashing.

PROTOCOL (reverse-engineered by others -- CITED, not guessed)
------------------------------------------------------------
Sources (fetched 2026-08-03):
  * baryluk/fnirsi-usb-power-data-logger  -- fnirsi_logger.py (PyUSB reference driver)
      https://github.com/baryluk/fnirsi-usb-power-data-logger
  * didim99/fnirsi-utils                  -- independent confirmation of framing + CRC
      https://github.com/didim99/fnirsi-utils
  * blakelton/FNIRSI-FNB-Web-Server, f4inu/FNB58 -- corroborate FNB58-class VID/PID+init

The FNB-C2 enumerates IDENTICALLY to the FNB58 (same VID 0x2E3C / PID 0x5558) and is
therefore treated as "FNB58-class" here. When plugged in it presents as a USB
composite device: MI_00 = CDC serial (COM8, silent on passive read), MI_02 = HID
(THE data interface we use). We select the HID interface by interface_number == 2,
falling back to a vendor usage_page, then the first HID collection.

Init handshake (three 64-byte HID OUTPUT reports; last byte of each is the frame CRC):
      AA 81 00*61 8E
      AA 82 00*61 96
      AA 82 00*61 96      <- FNB58-class repeats 0x82 here (non-58 devices send AA 83..9E)
Keepalive / "continue streaming" report, re-sent every ~1.0 s for FNB58-class:
      AA 83 00*61 9E

INPUT report (64 bytes, streamed ~100 samples/s => one report carries 4 samples):
      byte[0]      = 0xAA                     frame header
      byte[1]      = packet type (0x04 = data; anything else is ignored)
      byte[2..61]  = 4 samples x 15 bytes each
      byte[62]     = (counter/padding, covered by CRC)
      byte[63]     = CRC-8 of byte[1..62]

Per sample at offset = 2 + 15*i  (i = 0..3):
      voltage = u32_le(offset+0 .. offset+3) / 100000.0   -> volts   (raw is 10 uV/LSB)
      current = u32_le(offset+4 .. offset+7) / 100000.0   -> amps    (raw is 10 uA/LSB)
      d+      = u16_le(offset+8 .. offset+9) / 1000.0      -> volts
      d-      = u16_le(offset+10.. offset+11)/ 1000.0      -> volts
      temp_C  = u16_le(offset+13.. offset+14)/ 10.0        -> deg C
Current is reported UNSIGNED by the meter (orientation-independent); we treat it as a
magnitude. Energy accumulators are integrated host-side (the device does keep its own
running mAh/Wh but the raw HID stream does not expose them cleanly, so we integrate).

CRC-8 params (all frames): width=8 poly=0x39 init=0x42 refin=false refout=false xorout=0x00,
computed over byte[1 : -1] and compared against byte[-1].

DECODE IS DEFENSIVE ON PURPOSE
------------------------------
Because this file is authored WITHOUT hardware in the loop, the byte offsets above are
taken from the reference drivers but have NOT been re-verified against this exact unit.
Everything the orchestrator may need to correct is a named module-level constant
(FRAME_LEN, SAMPLE_BASE, SAMPLE_STRIDE, V_DIV, I_DIV, TEMP_*), and the frame locator
tolerates a report-ID prefix byte (some HID stacks prepend one on read/write). Turn on
--debug to dump raw reports and CRC pass/fail so a wrong offset is obvious in seconds.

hidapi DEPENDENCY
-----------------
Requires the 'hidapi' Python package (cython-hidapi), which provides `import hid`:
      pip install hidapi
(The similarly named 'hid' package by apmorton also exposes the same classic API and
works too.) On Windows a HID-class interface needs NO special driver (WinUSB/Zadig is
only for raw-USB/JTAG), so MI_02 is directly openable. If the package is missing we
print an actionable error and exit(2) rather than traceback.

USAGE
-----
  python tools/fnb_power_logger.py                       # stream forever, defaults
  python tools/fnb_power_logger.py --duration 7200       # 2 h soak then stop
  python tools/fnb_power_logger.py --interval 0.5        # twice-a-second live line/JSON
  python tools/fnb_power_logger.py --list                # list HID interfaces & exit
  python tools/fnb_power_logger.py --vid 0x2e3c --pid 0x5558
  python tools/fnb_power_logger.py --debug               # dump raw reports + CRC status
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import sys
import time
import collections
import statistics
from lab_paths import captures_dir

# ---------------------------------------------------------------------------
# Device identity (override with --vid/--pid). FNB-C2 == FNB58-class.
# ---------------------------------------------------------------------------
DEFAULT_VID = 0x2E3C
DEFAULT_PID = 0x5558
FNB58_CLASS_PIDS = (0x5558, 0x0049)   # FNB58, FNB48S -> repeat 0x82 in init, 1.0 s keepalive

# ---------------------------------------------------------------------------
# HID frame layout -- TUNABLE by the orchestrator against the real unit.
# ---------------------------------------------------------------------------
FRAME_LEN       = 64        # bytes in one streamed input report
FRAME_HEADER    = 0xAA      # byte[0]
PKT_TYPE_DATA   = 0x04      # byte[1] value that marks a data frame
SAMPLE_BASE     = 2         # first sample starts here
SAMPLE_STRIDE   = 15        # bytes per sample
SAMPLES_PER_PKT = 4         # samples packed per report
V_DIV           = 100000.0  # raw -> volts
I_DIV           = 100000.0  # raw -> amps
DP_DN_DIV       = 1000.0    # raw -> volts (USB data lines)
TEMP_OFFSET     = 13        # within-sample offset of the u16 temperature
TEMP_DIV        = 10.0      # raw -> deg C
SAMPLE_RATE_SPS = 100       # nominal samples/second (used as a dt fallback only)

# CRC-8 (used only to validate received frames; init frames are hard-coded below).
CRC_POLY = 0x39
CRC_INIT = 0x42

# ---------------------------------------------------------------------------
# Init / keepalive frames (verbatim from the reference drivers). Last byte = CRC.
# ---------------------------------------------------------------------------
def _frame(second_byte: int, crc: int) -> bytes:
    return bytes([FRAME_HEADER, second_byte]) + b"\x00" * 61 + bytes([crc])

INIT_FRAME_81 = _frame(0x81, 0x8E)
INIT_FRAME_82 = _frame(0x82, 0x96)
INIT_FRAME_83 = _frame(0x83, 0x9E)
CONT_FRAME    = INIT_FRAME_83          # "continue streaming" keepalive
KEEPALIVE_S   = 1.0                    # FNB58-class refresh cadence (non-58: ~0.003 s)

# ---------------------------------------------------------------------------
# PSU-risk thresholds -- FIRST GUESS for a single XIAO ESP32-C6 + meter node on a
# 5 V bus. Kept IDENTICAL to tools/lab_e2e_monitor.py (PSU_WARN_MA / PSU_BLOCK_MA) so
# the FNB CSV and the e2e dashboard agree on OK/WARN/BLOCK for the same current.
# Rationale (see MEMORY "LwM2M burst = USB cliff"): the fleet's chronic failure is a
# USB-HOST over-current shutdown on TX bursts, and a USB-2.0 host guarantees only
# 500 mA -- so a peak approaching that ceiling is the danger signal.
#   OK   : peak < 350 mA   (idle + normal Thread/LwM2M chatter, headroom intact)
#   WARN : 350-480 mA      (headroom shrinking -- watch for the cliff)
#   BLOCK: > 480 mA        (within ~4% of the 500 mA USB-2.0 host limit)
# psu_risk is derived from PEAK current (worst case seen), not the instantaneous value.
# Tune to your host/hub once the real idle/peak envelope of the node is measured.
# ---------------------------------------------------------------------------
PSU_WARN_MA  = 350.0
PSU_BLOCK_MA = 480.0
BUS_NOMINAL_V = 5.0

# Sanity clamp: if wall-clock between two reports jumps (stall / reconnect), don't
# integrate a bogus huge dt. Cap per-report dt to this many nominal report-periods.
MAX_REPORT_DT_S = 2.0

# Median filter width for single-sample outlier rejection: a CRC-valid FNB report can
# still carry one garbage sample (observed ~1/500). A median-of-N rejects an ISOLATED
# spike while a genuine sustained event (>=2 consecutive samples, >=~20 ms at 100 Hz)
# passes unchanged. Applied to the current used for i_ma, i_peak and mAh/Wh integration
# so one bad sample can't trip a false BLOCK.
MEDIAN_WINDOW = 3
OUTLIER_DELTA_MA = 150.0     # raw sample this far from the median = a rejected glitch

_HERE = os.path.dirname(os.path.abspath(__file__))


# ===========================================================================
# CRC-8 (non-reflected, MSB-first). poly=0x39 init=0x42 refin/out=false xorout=0.
# ===========================================================================
def crc8(data: bytes, poly: int = CRC_POLY, init: int = CRC_INIT) -> int:
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def _u32_le(buf, off: int) -> int:
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16) | (buf[off + 3] << 24)


def _u16_le(buf, off: int) -> int:
    return buf[off] | (buf[off + 1] << 8)


# ===========================================================================
# Frame location + decode. Robust to an optional leading report-ID byte and to
# a wrong start offset: we scan candidate starts and prefer one whose CRC checks.
# ===========================================================================
def locate_frame(raw):
    """Return (start_index, crc_ok) for the best 0xAA data frame in `raw`, or (None, False).

    Some HID backends prepend a report-ID byte on read, shifting the whole frame by
    one; others don't. We try every 0xAA position that leaves a full FRAME_LEN window
    and pick the first whose CRC validates. If none validate we still return the first
    0xAA with a plausible data type so decode can proceed (and --debug will show the
    CRC miss), because a mis-documented offset must not silently drop all data."""
    n = len(raw)
    fallback = None
    for start in range(0, n - FRAME_LEN + 1):
        if raw[start] != FRAME_HEADER:
            continue
        frame = raw[start:start + FRAME_LEN]
        crc_ok = (crc8(bytes(frame[1:-1])) == frame[-1])
        if crc_ok:
            return start, True
        if fallback is None and frame[1] == PKT_TYPE_DATA:
            fallback = start
    if fallback is not None:
        return fallback, False
    return None, False


def decode_frame(raw, debug: bool = False):
    """Decode one HID input report -> list of (voltage_V, current_A, temp_C) samples.

    Returns [] for non-data / unlocatable frames (caller just skips them)."""
    start, crc_ok = locate_frame(raw)
    if start is None:
        if debug:
            print("  [decode] no 0xAA frame in %d bytes: %s"
                  % (len(raw), bytes(raw[:16]).hex()), file=sys.stderr)
        return []
    frame = raw[start:start + FRAME_LEN]
    if frame[1] != PKT_TYPE_DATA:
        return []
    if debug and not crc_ok:
        print("  [decode] CRC MISMATCH (offset/doc may be wrong) frame=%s"
              % bytes(frame).hex(), file=sys.stderr)

    samples = []
    for i in range(SAMPLES_PER_PKT):
        off = SAMPLE_BASE + SAMPLE_STRIDE * i
        if off + SAMPLE_STRIDE > FRAME_LEN:
            break
        voltage = _u32_le(frame, off + 0) / V_DIV
        current = _u32_le(frame, off + 4) / I_DIV
        temp_c = _u16_le(frame, off + TEMP_OFFSET) / TEMP_DIV
        samples.append((voltage, abs(current), temp_c))
    return samples


# ===========================================================================
# Accumulator: cumulative state that MUST survive reconnects.
# ===========================================================================
class Accumulator:
    def __init__(self):
        self.v_bus = 0.0        # latest bus voltage (V)
        self.i_ma = 0.0         # latest current (mA)
        self.p_w = 0.0          # latest power (W)
        self.temp_c = 0.0       # latest temperature (deg C)
        self.mah_cum = 0.0      # integrated charge (mAh)
        self.wh_cum = 0.0       # integrated energy (Wh)
        self.i_peak_ma = 0.0    # peak current seen (mA)
        self.samples = 0        # total samples decoded
        self.n_outliers = 0     # single-sample decode spikes rejected by the median filter
        self._last_wall = None  # wall clock of previous report (for dt integration)
        self._i_window = collections.deque(maxlen=MEDIAN_WINDOW)  # raw current, outlier reject

    def add_report(self, samples):
        """Integrate one report's worth of samples using measured wall-clock dt,
        distributed evenly across the samples in the report. Falls back to the
        nominal rate for the first report or after a stall (clamped)."""
        if not samples:
            return
        now = time.time()
        n = len(samples)
        if self._last_wall is None:
            dt_total = n / float(SAMPLE_RATE_SPS)
        else:
            dt_total = now - self._last_wall
            # Guard against negative/zero clocks and post-stall jumps.
            if dt_total <= 0 or dt_total > MAX_REPORT_DT_S:
                dt_total = n / float(SAMPLE_RATE_SPS)
        self._last_wall = now
        dt_each = dt_total / n

        for (voltage, current_a, temp_c) in samples:
            raw_ma = current_a * 1000.0
            self._i_window.append(raw_ma)
            # Median-of-N filter rejects an isolated single-sample decode spike (a
            # CRC-valid frame can still carry one garbage sample). Used for the reported
            # current, peak and integration so one bad sample can't trip a false BLOCK.
            current_ma = statistics.median(self._i_window)
            if len(self._i_window) >= MEDIAN_WINDOW and abs(raw_ma - current_ma) > OUTLIER_DELTA_MA:
                self.n_outliers += 1
            power_w = voltage * (current_ma / 1000.0)
            self.v_bus = voltage
            self.i_ma = current_ma
            self.p_w = power_w
            self.temp_c = temp_c
            if current_ma > self.i_peak_ma:
                self.i_peak_ma = current_ma
            # charge/energy: mA*h and W*h -> divide seconds by 3600
            self.mah_cum += current_ma * (dt_each / 3600.0)
            self.wh_cum += power_w * (dt_each / 3600.0)
            self.samples += 1

    def psu_risk(self) -> str:
        p = self.i_peak_ma
        if p > PSU_BLOCK_MA:
            return "BLOCK"
        if p > PSU_WARN_MA:
            return "WARN"
        return "OK"

    def power_block(self) -> dict:
        """The canonical lab-snapshot 'power' block (schema-verbatim keys)."""
        return {
            "source": "FNB-C2",
            "v_bus": round(self.v_bus, 3),
            "i_ma": round(self.i_ma, 1),
            "p_w": round(self.p_w, 3),
            "mah_cum": round(self.mah_cum, 4),
            "wh_cum": round(self.wh_cum, 5),
            "i_peak_ma": round(self.i_peak_ma, 1),
            "samples": self.samples,
            "psu_risk": self.psu_risk(),
        }


# ===========================================================================
# HID transport
# ===========================================================================
def _import_hid():
    try:
        import hid  # noqa: F401
        return hid
    except ImportError:
        sys.stderr.write(
            "\nERROR: the 'hidapi' Python package is required to talk to the FNB-C2.\n"
            "       Install it into the venv, e.g.:\n"
            "         C:/Users/jsgir/Documents/ESP32/.venv/Scripts/python.exe -m pip install hidapi\n"
            "       (the 'hid' package by apmorton also works). Then re-run.\n\n")
        sys.exit(2)


def list_interfaces(hid, vid: int, pid: int):
    infos = hid.enumerate(vid, pid)
    if not infos:
        print("No HID interfaces found for VID 0x%04x PID 0x%04x. "
              "Is the FNB-C2 plugged in and not held by another program?" % (vid, pid))
        return
    print("HID interfaces for VID 0x%04x PID 0x%04x:" % (vid, pid))
    for d in infos:
        print("  if=%s usage_page=0x%04x usage=0x%04x product=%r serial=%r path=%r" % (
            d.get("interface_number"),
            d.get("usage_page") or 0,
            d.get("usage") or 0,
            d.get("product_string"),
            d.get("serial_number"),
            d.get("path"),
        ))


def _pick_interface(infos):
    """Choose the DATA HID interface: MI_02 first, then a vendor usage_page, then first."""
    for d in infos:
        if d.get("interface_number") == 2:
            return d
    for d in infos:
        if (d.get("usage_page") or 0) >= 0xFF00:
            return d
    return infos[0] if infos else None


def open_device(hid, vid: int, pid: int):
    """Open and return an opened hid.device for the FNB-C2 data interface, or None."""
    infos = hid.enumerate(vid, pid)
    if not infos:
        return None
    chosen = _pick_interface(infos)
    if chosen is None:
        return None
    dev = hid.device()
    try:
        dev.open_path(chosen["path"])
    except Exception:
        # Fallback: open by VID/PID (grabs the first/default collection).
        try:
            dev.open(vid, pid)
        except Exception:
            return None
    try:
        dev.set_nonblocking(0)  # blocking reads with per-read timeout (see read(...))
    except Exception:
        pass
    return dev


def hid_write(dev, payload: bytes):
    """Write a 64-byte report. On Windows the buffer's first byte is the report ID
    (0x00 for a single-report device), so we prepend it; if that specific write is
    rejected we retry without the prefix. Returns bytes written (or -1)."""
    try:
        return dev.write([0x00] + list(payload))
    except Exception:
        try:
            return dev.write(list(payload))
        except Exception:
            return -1


def send_init(dev, fnb58_class: bool):
    """Send the documented handshake so the meter starts streaming."""
    hid_write(dev, INIT_FRAME_81)
    hid_write(dev, INIT_FRAME_82)
    hid_write(dev, INIT_FRAME_82 if fnb58_class else INIT_FRAME_83)


# ===========================================================================
# CSV + JSON sinks
# ===========================================================================
CSV_HEADER = ["ts_iso", "t_rel_s", "v_bus", "i_ma", "p_w",
              "mah_cum", "wh_cum", "i_peak_ma", "temp_c", "psu_risk"]


def open_csv(path: str):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    f = open(path, "a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if new:
        w.writerow(CSV_HEADER)
        f.flush()
    return f, w


def write_latest_json(path: str, block: dict):
    """Atomic-ish rolling write of the 'power' block so readers never see a torn file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(block, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        sys.stderr.write("[json] write failed: %s\n" % e)


# ===========================================================================
# Main streaming loop (reconnect-safe; cumulative state persists across drops)
# ===========================================================================
def run(args):
    hid = _import_hid()

    if args.list:
        list_interfaces(hid, args.vid, args.pid)
        return 0

    fnb58_class = args.pid in FNB58_CLASS_PIDS

    # CSV filename: explicit --csv, else timestamped under tools/.
    if args.csv:
        csv_path = args.csv
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(str(captures_dir()), "fnb_power_%s.csv" % stamp)
    json_path = args.json_out or os.path.join(str(captures_dir()), "fnb_latest.json")

    csv_file, csv_w = open_csv(csv_path)
    acc = Accumulator()

    t0 = time.time()
    last_ka = 0.0          # last keepalive send
    last_ui = 0.0          # last live-line + JSON write
    consecutive_timeouts = 0
    dev = None

    print("fnb_power_logger: VID 0x%04x PID 0x%04x (%s)  csv=%s  json=%s" % (
        args.vid, args.pid, "FNB58-class" if fnb58_class else "FNB48/C1-class",
        csv_path, json_path))
    print("PSU thresholds (peak mA on %.1f V): OK<%.0f  WARN %.0f-%.0f  BLOCK>%.0f" % (
        BUS_NOMINAL_V, PSU_WARN_MA, PSU_WARN_MA, PSU_BLOCK_MA, PSU_BLOCK_MA))

    try:
        while True:
            # ---- duration check ----
            if args.duration and (time.time() - t0) >= args.duration:
                print("\nreached --duration %.0fs, stopping." % args.duration)
                break

            # ---- (re)connect ----
            if dev is None:
                dev = open_device(hid, args.vid, args.pid)
                if dev is None:
                    # No device: emit a graceful "source down" JSON so downstream
                    # readers see the logger is alive but the meter isn't, then wait.
                    _emit_source_down(json_path, acc)
                    _live_line(acc, time.time() - t0, connected=False)
                    time.sleep(2.0)
                    continue
                send_init(dev, fnb58_class)
                last_ka = time.time()
                consecutive_timeouts = 0
                print("[hid] connected + init sent.")

            # ---- keepalive ----
            now = time.time()
            if now - last_ka >= KEEPALIVE_S:
                hid_write(dev, CONT_FRAME)
                last_ka = now

            # ---- read one report (100 ms timeout so keepalive/UI stay responsive) ----
            try:
                raw = dev.read(FRAME_LEN + 1, 100)   # +1 tolerates a report-ID prefix
            except Exception as e:
                sys.stderr.write("[hid] read error: %s -- reconnecting\n" % e)
                _safe_close(dev)
                dev = None
                time.sleep(1.0)
                continue

            if not raw:
                consecutive_timeouts += 1
                # ~5 s of total silence => stream likely wedged; force a reconnect.
                if consecutive_timeouts >= 50:
                    sys.stderr.write("[hid] no data for ~5 s -- reconnecting\n")
                    _safe_close(dev)
                    dev = None
                    consecutive_timeouts = 0
            else:
                consecutive_timeouts = 0
                if args.debug:
                    print("  [raw] %s" % bytes(raw).hex(), file=sys.stderr)
                samples = decode_frame(raw, debug=args.debug)
                if samples:
                    acc.add_report(samples)
                    ts_iso = datetime.datetime.now().isoformat(timespec="seconds")
                    t_rel = time.time() - t0
                    risk = acc.psu_risk()
                    for (voltage, current_a, temp_c) in samples:
                        csv_w.writerow([
                            ts_iso, "%.3f" % t_rel,
                            "%.4f" % voltage, "%.2f" % (current_a * 1000.0),
                            "%.4f" % (voltage * current_a),
                            "%.5f" % acc.mah_cum, "%.6f" % acc.wh_cum,
                            "%.2f" % acc.i_peak_ma, "%.1f" % temp_c, risk,
                        ])

            # ---- periodic UI + JSON + CSV flush ----
            now = time.time()
            if now - last_ui >= args.interval:
                last_ui = now
                write_latest_json(json_path, acc.power_block())
                try:
                    csv_file.flush()
                except Exception:
                    pass
                _live_line(acc, now - t0, connected=True)

    except KeyboardInterrupt:
        print("\ninterrupted by user.")
    finally:
        # Final flush so nothing is lost on exit.
        try:
            write_latest_json(json_path, acc.power_block())
        except Exception:
            pass
        try:
            csv_file.flush()
            csv_file.close()
        except Exception:
            pass
        _safe_close(dev)
        print("summary: samples=%d  peak=%.1f mA  charge=%.3f mAh  energy=%.4f Wh  risk=%s"
              % (acc.samples, acc.i_peak_ma, acc.mah_cum, acc.wh_cum, acc.psu_risk()))
    return 0


def _emit_source_down(json_path: str, acc: Accumulator):
    """Write a schema-shaped block with live=false so readers can show 'meter down'
    without the numbers going stale-but-plausible. Cumulative counters are preserved."""
    block = acc.power_block()
    block["v_bus"] = None
    block["i_ma"] = None
    block["p_w"] = None
    block["psu_risk"] = "OK"   # no current flowing that we can see
    write_latest_json(json_path, block)


def _live_line(acc: Accumulator, t_rel: float, connected: bool):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    if not connected:
        print("[%s] meter DISCONNECTED -- waiting  (cum: %.3f mAh / %.4f Wh, n=%d)"
              % (ts, acc.mah_cum, acc.wh_cum, acc.samples))
        return
    print("[%s] Vbus=%6.3fV  I=%7.1fmA  P=%6.3fW  T=%4.1fC | peak=%7.1fmA "
          "mAh=%8.3f Wh=%7.4f n=%-7d risk=%s"
          % (ts, acc.v_bus, acc.i_ma, acc.p_w, acc.temp_c,
             acc.i_peak_ma, acc.mah_cum, acc.wh_cum, acc.samples, acc.psu_risk()))


def _safe_close(dev):
    if dev is not None:
        try:
            dev.close()
        except Exception:
            pass


def _auto_int(s: str) -> int:
    """argparse type: accept 0x2e3c or 11836."""
    return int(s, 0)


def build_parser():
    ap = argparse.ArgumentParser(
        description="Continuous power/consumption logger for the FNIRSI FNB-C2 "
                    "(HID, VID 0x2E3C / PID 0x5558). Streams V/I at ~100 Hz, "
                    "integrates mAh/Wh, tracks peak current, writes CSV + rolling JSON.")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="seconds to run; 0 = forever (default)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="live-line + JSON refresh cadence in seconds (default 1.0)")
    ap.add_argument("--csv", default=None,
                    help="CSV path (default: tools/fnb_power_<timestamp>.csv)")
    ap.add_argument("--json-out", dest="json_out", default=None,
                    help="rolling latest JSON path (default: tools/fnb_latest.json)")
    ap.add_argument("--vid", type=_auto_int, default=DEFAULT_VID,
                    help="USB vendor ID (default 0x2E3C)")
    ap.add_argument("--pid", type=_auto_int, default=DEFAULT_PID,
                    help="USB product ID (default 0x5558)")
    ap.add_argument("--list", action="store_true",
                    help="list matching HID interfaces (with interface_number/usage) and exit")
    ap.add_argument("--debug", action="store_true",
                    help="dump raw HID reports + CRC pass/fail (use to verify field offsets)")
    return ap


def main():
    args = build_parser().parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
