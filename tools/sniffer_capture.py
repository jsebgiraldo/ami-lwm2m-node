"""AMI 802.15.4 sniffer — Wireshark extcap plugin + standalone capture tool.

The sniffer firmware (tools/sniffer/, flashed to the ESP32-C6 DevKit on
COM62) emits one line per captured frame:

    $<hexframe>|<rssi_dbm>|<lqi>

plus a one-shot boot banner:

    #AMI-SNIFFER channel=<n> promiscuous=1

This script wraps each frame into a DLT_IEEE802_15_4_TAP (link type 283)
PCAP record — the modern format Wireshark prefers, carrying per-frame
RSSI / LQI / channel as TLV metadata. Frames arrive FCS-stripped (the
ESP32 HAL validates+strips the FCS), so the TAP header tags FCS-type = 0.

Two ways to run it
------------------
1. As a Wireshark extcap plugin (recommended): install via
   tools/install_extcap.py — then "AMI 802.15.4 Sniffer" shows up directly
   in Wireshark's interface list, configurable from the GUI.

2. Standalone from the shell:
       python tools/sniffer_capture.py --com COM62 --pcap mesh.pcap --stats
       python tools/sniffer_capture.py --com COM62 -w - | wireshark -k -i -
"""
from __future__ import annotations

import argparse
import struct
import sys
import time

# Detect an extcap invocation *before* touching anything venv-related:
# when Wireshark runs this as a plugin we must NOT re-exec under a
# different interpreter. The .bat wrapper installed in Wireshark's extcap
# directory already pins the project venv (which has pyserial).
_EXTCAP_FLAGS = ("--extcap-interfaces", "--extcap-dlts", "--extcap-config",
                 "--extcap-version", "--extcap-reload-option", "--capture")
_IS_EXTCAP = any(a == f or a.startswith(f + "=")
                 for a in sys.argv for f in _EXTCAP_FLAGS)

try:
    import serial
except ImportError:
    if _IS_EXTCAP:
        sys.stderr.write("AMI sniffer extcap: pyserial not available to "
                         "this Python interpreter\n")
        sys.exit(1)
    # Standalone mode — bootstrap into the project venv like the other tools.
    import fleet_common as fc
    fc.bootstrap_venv()
    import serial  # noqa: E402

LINKTYPE_IEEE802154_TAP = 283
DEFAULT_BAUD = 1000000
DEFAULT_COM = "COM62"
DEFAULT_CHANNEL = 21
EXTCAP_IFACE = "ami-sniffer"

# --- IEEE 802.15.4 TAP TLV types (see Wireshark packet-ieee802154.c) ---
TAP_FCS_TYPE = 0     # uint8: 0 = none/not present, 1 = 16-bit CRC
TAP_RSS = 1          # float32 LE, dBm
TAP_CHANNEL = 3      # uint16 channel + uint8 page
TAP_LQI = 10         # uint8


# ───────────────────────── PCAP / TAP encoding ──────────────────────────
def pcap_global_header() -> bytes:
    # magic, ver_major, ver_minor, thiszone, sigfigs, snaplen, network
    return struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535,
                       LINKTYPE_IEEE802154_TAP)


def _tlv(tlv_type: int, value: bytes) -> bytes:
    """One TAP TLV: type, length, value zero-padded to a 4-byte boundary."""
    pad = (-len(value)) % 4
    return struct.pack("<HH", tlv_type, len(value)) + value + (b"\x00" * pad)


def tap_header(rssi_dbm: int, lqi: int, channel: int) -> bytes:
    tlvs = b""
    tlvs += _tlv(TAP_FCS_TYPE, struct.pack("<B", 0))           # FCS not present
    tlvs += _tlv(TAP_RSS, struct.pack("<f", float(rssi_dbm)))
    tlvs += _tlv(TAP_CHANNEL, struct.pack("<HB", channel, 0))  # page 0
    tlvs += _tlv(TAP_LQI, struct.pack("<B", lqi & 0xFF))
    total = 4 + len(tlvs)  # 4-byte fixed header + TLVs (4 + 32 = 36, % 4 == 0)
    # version=0, reserved=0, length
    return struct.pack("<BBH", 0, 0, total) + tlvs


def pcap_record(payload: bytes) -> bytes:
    t = time.time()
    sec = int(t)
    usec = int((t - sec) * 1_000_000)
    return struct.pack("<IIII", sec, usec, len(payload), len(payload)) + payload


# --- 802.15.4 frame-type decode, only for the --stats line ---
_FRAME_TYPES = {0: "Beacon", 1: "Data", 2: "Ack", 3: "MAC-Cmd",
                4: "Reserved", 5: "Multipurpose", 6: "Frag", 7: "Extended"}


def frame_type(frame: bytes) -> str:
    if not frame:
        return "Empty"
    return _FRAME_TYPES.get(frame[0] & 0x07, "?")


# ───────────────────────── capture core ─────────────────────────────────
def run_capture(com: str, baud: int, channel: int, out, stats: bool) -> int:
    """Read frames from the sniffer board and stream PCAP records to `out`.

    `out` is any binary, writable stream (a file, sys.stdout.buffer, or a
    Wireshark extcap FIFO). Runs until the serial port dies, the output
    pipe breaks (Wireshark stopped the capture), or Ctrl-C.
    """
    try:
        ser = serial.Serial(com, baud, timeout=1)
    except serial.SerialException as e:
        sys.stderr.write(f"[sniffer] cannot open {com} @ {baud}: {e}\n")
        return 1

    sys.stderr.write(f"[sniffer] capturing from {com} @ {baud} "
                     f"(DLT_IEEE802_15_4_TAP, channel label {channel})\n")
    sys.stderr.flush()

    try:
        out.write(pcap_global_header())
        out.flush()
    except (BrokenPipeError, OSError) as e:
        sys.stderr.write(f"[sniffer] output closed before start: {e}\n")
        return 1

    n_frames = n_bad = 0
    type_counts: dict[str, int] = {}
    last_stats = time.time()
    last_count = 0

    try:
        while True:
            raw = ser.readline()
            if not raw:
                if stats and time.time() - last_stats >= 5.0:
                    _emit_stats(n_frames, last_count, type_counts, last_stats)
                    last_stats, last_count = time.time(), n_frames
                continue

            line = raw.decode("ascii", errors="replace").strip()

            if line.startswith("#AMI-SNIFFER"):
                for tok in line.split():
                    if tok.startswith("channel="):
                        try:
                            channel = int(tok.split("=", 1)[1])
                        except ValueError:
                            pass
                sys.stderr.write(f"[sniffer] board: {line} "
                                 f"(channel {channel})\n")
                sys.stderr.flush()
                continue

            if not line.startswith("$"):
                continue  # firmware log noise — ignore

            try:
                hexframe, rssi_s, lqi_s = line[1:].split("|")
                frame = bytes.fromhex(hexframe)
                rssi = int(rssi_s)
                lqi = int(lqi_s)
            except (ValueError, IndexError):
                n_bad += 1
                continue
            # The ESP32 IEEE 802.15.4 HAL replaces the on-air FCS with a
            # [rssi][lqi] tail, and in RAW mode the driver hands the whole
            # buffer up — so every frame carries 2 trailing bytes that are
            # NOT part of the 802.15.4 frame. rssi/lqi are already parsed
            # from the metadata fields above; drop the redundant tail so
            # the PCAP frame is clean MHR+payload (TAP FCS-type = 0).
            if len(frame) > 2:
                frame = frame[:-2]
            if not frame:
                n_bad += 1
                continue

            out.write(pcap_record(tap_header(rssi, lqi, channel) + frame))
            out.flush()
            n_frames += 1

            if stats:
                ft = frame_type(frame)
                type_counts[ft] = type_counts.get(ft, 0) + 1
                if time.time() - last_stats >= 5.0:
                    _emit_stats(n_frames, last_count, type_counts, last_stats)
                    last_stats, last_count = time.time(), n_frames

    except KeyboardInterrupt:
        sys.stderr.write(f"\n[sniffer] stopped — {n_frames} frames "
                         f"({n_bad} malformed lines ignored)\n")
    except (BrokenPipeError, OSError):
        # Wireshark closed the extcap FIFO — normal "stop capture".
        sys.stderr.write(f"[sniffer] capture closed — {n_frames} frames\n")
    finally:
        ser.close()
        try:
            if out not in (sys.stdout.buffer,):
                out.close()
        except Exception:
            pass
    return 0


def _emit_stats(total: int, last_count: int, type_counts: dict[str, int],
                last_stats: float) -> None:
    dt = max(time.time() - last_stats, 1e-6)
    rate = (total - last_count) / dt
    types = "  ".join(f"{k}:{v}" for k, v in sorted(type_counts.items()))
    sys.stderr.write(f"[sniffer] {total} frames  ({rate:.1f}/s)   {types}\n")
    sys.stderr.flush()


# ───────────────────────── extcap protocol ──────────────────────────────
def extcap_interfaces() -> None:
    print("extcap {version=1.0}{help=https://github.com/UNAL/ami-lwm2m-node}"
          "{display=AMI 802.15.4 Sniffer}")
    print(f"interface {{value={EXTCAP_IFACE}}}"
          f"{{display=AMI 802.15.4 Sniffer (ESP32-C6)}}")


def extcap_dlts() -> None:
    print(f"dlt {{number={LINKTYPE_IEEE802154_TAP}}}"
          f"{{name=IEEE802_15_4_TAP}}{{display=IEEE 802.15.4 TAP}}")


def extcap_config() -> None:
    print(f"arg {{number=0}}{{call=--com}}{{display=Serial port}}"
          f"{{tooltip=COM port of the AMI sniffer board (CH343 DevKit)}}"
          f"{{type=string}}{{default={DEFAULT_COM}}}{{required=true}}")
    print(f"arg {{number=1}}{{call=--baud}}{{display=Baud rate}}"
          f"{{tooltip=Must match tools/sniffer/app.overlay}}"
          f"{{type=integer}}{{default={DEFAULT_BAUD}}}")
    print(f"arg {{number=2}}{{call=--channel}}{{display=Channel label}}"
          f"{{tooltip=802.15.4 channel - PCAP metadata only; the firmware "
          f"channel is fixed at build time}}"
          f"{{type=integer}}{{default={DEFAULT_CHANNEL}}}"
          f"{{range=11,26}}")


def extcap_capture(fifo: str, com: str, baud: int, channel: int) -> int:
    # On Windows the FIFO is a named pipe Wireshark already created; on
    # POSIX it's a mkfifo path. open(..., 'wb') works for both.
    try:
        out = open(fifo, "wb")
    except OSError as e:
        sys.stderr.write(f"[sniffer] cannot open extcap fifo {fifo}: {e}\n")
        return 1
    return run_capture(com, baud, channel, out, stats=False)


# ───────────────────────── argument parsing ─────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)

    # Standalone options
    ap.add_argument("--com", default=DEFAULT_COM,
                    help=f"serial port of the sniffer board (default {DEFAULT_COM})")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                    help=f"baud rate (default {DEFAULT_BAUD})")
    ap.add_argument("--channel", type=int, default=DEFAULT_CHANNEL,
                    help="channel label for PCAP metadata (default 21)")
    ap.add_argument("-w", "--pcap", default="-",
                    help="PCAP output path, or '-' for stdout (default '-')")
    ap.add_argument("--stats", action="store_true",
                    help="print a rolling stats line to stderr every 5s")

    # extcap protocol options (hidden from --help; driven by Wireshark)
    ap.add_argument("--extcap-interfaces", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--extcap-dlts", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--extcap-config", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--extcap-version", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--extcap-interface", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--capture", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--fifo", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--extcap-control-in", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--extcap-control-out", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--extcap-reload-option", default=None, help=argparse.SUPPRESS)
    return ap


def main() -> int:
    args = build_parser().parse_args()

    # --- extcap protocol phases (invoked by Wireshark) ---
    if args.extcap_interfaces:
        extcap_interfaces()
        return 0
    if args.extcap_dlts:
        extcap_dlts()
        return 0
    if args.extcap_config:
        extcap_config()
        return 0
    if args.capture:
        if not args.fifo:
            sys.stderr.write("[sniffer] --capture requires --fifo\n")
            return 1
        return extcap_capture(args.fifo, args.com, args.baud, args.channel)

    # --- standalone mode ---
    if args.pcap == "-":
        out = sys.stdout.buffer
    else:
        try:
            out = open(args.pcap, "wb")
        except OSError as e:
            sys.stderr.write(f"[sniffer] cannot open {args.pcap}: {e}\n")
            return 1
    return run_capture(args.com, args.baud, args.channel, out, args.stats)


if __name__ == "__main__":
    sys.exit(main())
