"""AD2 brownout capture: trigger on 3V3 rail dip, save CSV + report.

Hookup (SuperMini):
  AD2 Scope CH1 (1+ orange / 1- orange-white) -> board 3V3 pin / GND
  AD2 Scope CH2 (2+ blue   / 2- blue-white)   -> board 5V pin  / GND
  AD2 GND ---------------------------------------> board GND

Behavior:
  * Configures both analog inputs +-5 V range.
  * Trigger: CH1 falling edge at 2.9 V (BOD threshold is 2.51 V, so we
    capture the ramp-down BEFORE the chip actually resets).
  * Sample rate 1 MHz, 8192-sample buffer = 8.192 ms window with 20%
    pre-trigger (~1.6 ms before the dip, ~6.5 ms after).
  * On each captured frame: prints dip metrics (min V, dip duration,
    settle time), saves raw CSV.

Usage:
  python tools/ad2_brownout_capture.py             # capture 1 frame
  python tools/ad2_brownout_capture.py --frames 5  # capture 5 frames
  python tools/ad2_brownout_capture.py --timeout 600  # wait up to 10 min

Note: requires WaveForms desktop app's Scope tab CLOSED — only one
client can hold the device at a time. Run dwf.dll path
(C:/Program Files/Digilent/WaveForms3/dwf.dll) must exist.
"""
import argparse
import csv
import ctypes
import datetime
import os
import sys
import time
from ctypes import c_int, c_double, c_uint, c_byte, byref, create_string_buffer

DWF_DLL = "C:/Program Files/Digilent/WaveForms3/dwf.dll"

# --- Constants from dwf.h (subset needed) ---
hdwfNone = c_int(0)
DwfStateDone = c_byte(2)

trigsrcDetectorAnalogIn = c_byte(2)
trigtypeEdge = c_int(0)
trigcondFallingNegative = c_int(1)
trigcondRisingPositive = c_int(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=1, help="Number of trigger captures")
    ap.add_argument("--trigger-v", type=float, default=2.9, help="CH1 falling-edge trigger level (V)")
    ap.add_argument("--rate-hz", type=float, default=1e6, help="Sample rate (Hz)")
    ap.add_argument("--samples", type=int, default=8192, help="Buffer depth")
    ap.add_argument("--range-v", type=float, default=5.0, help="Channel input range (Vpp)")
    ap.add_argument("--pre", type=float, default=0.2, help="Pre-trigger fraction (0..1)")
    ap.add_argument("--timeout", type=float, default=300.0, help="Wait per frame (s)")
    ap.add_argument("--outdir", default="logs/ad2", help="Output directory")
    ap.add_argument("--swap", action="store_true",
                    help="Wiring is CH1=5V, CH2=3V3 (default assumes CH1=3V3, CH2=5V)")
    args = ap.parse_args()
    # Which AD2 channel index holds the 3V3 / 5V rail.
    ch3v3 = 1 if args.swap else 0
    ch5v  = 0 if args.swap else 1

    if not os.path.exists(DWF_DLL):
        sys.exit(f"dwf.dll not found at {DWF_DLL}")
    dwf = ctypes.cdll.LoadLibrary(DWF_DLL)

    version = create_string_buffer(32)
    dwf.FDwfGetVersion(version)
    print(f"[ad2] dwf version: {version.value.decode()}")

    hdwf = c_int()
    if dwf.FDwfDeviceOpen(c_int(-1), byref(hdwf)) == 0:
        err = create_string_buffer(512)
        dwf.FDwfGetLastErrorMsg(err)
        sys.exit(f"[ad2] FDwfDeviceOpen failed: {err.value.decode()}\n"
                 f"      Close WaveForms Scope and retry, the GUI holds the device.")
    print(f"[ad2] device opened (handle={hdwf.value})")

    # Both channels: 25 V range, offset 0 — comfortable headroom for 5 V and
    # 3.3 V rails, no clipping. AD2 14-bit ADC at 25 V range gives ~1.5 mV
    # resolution which is plenty for brownout characterization.
    for ch in (ch3v3, ch5v):
        dwf.FDwfAnalogInChannelEnableSet(hdwf, c_int(ch), c_int(1))
        dwf.FDwfAnalogInChannelRangeSet(hdwf, c_int(ch), c_double(25.0))
        dwf.FDwfAnalogInChannelOffsetSet(hdwf, c_int(ch), c_double(0.0))

    dwf.FDwfAnalogInFrequencySet(hdwf, c_double(args.rate_hz))
    dwf.FDwfAnalogInBufferSizeSet(hdwf, c_int(args.samples))

    # Trigger: falling edge on CH1 at args.trigger_v
    dwf.FDwfAnalogInTriggerSourceSet(hdwf, trigsrcDetectorAnalogIn)
    dwf.FDwfAnalogInTriggerTypeSet(hdwf, trigtypeEdge)
    dwf.FDwfAnalogInTriggerChannelSet(hdwf, c_int(ch3v3))
    dwf.FDwfAnalogInTriggerConditionSet(hdwf, trigcondFallingNegative)
    dwf.FDwfAnalogInTriggerLevelSet(hdwf, c_double(args.trigger_v))
    dwf.FDwfAnalogInTriggerHysteresisSet(hdwf, c_double(0.05))
    window_s = args.samples / args.rate_hz
    pre_s = window_s * args.pre
    # FDwfAnalogInTriggerPositionSet: time at trigger relative to buffer center.
    # If buffer is W and we want pre fraction = p, position from center = (0.5 - p) * W.
    pos = (0.5 - args.pre) * window_s
    dwf.FDwfAnalogInTriggerPositionSet(hdwf, c_double(pos))
    print(f"[ad2] window={window_s*1000:.2f} ms  pre-trigger={pre_s*1000:.2f} ms  "
          f"trigger=CH{ch3v3+1} (3V3) falling {args.trigger_v} V")

    os.makedirs(args.outdir, exist_ok=True)

    for i in range(args.frames):
        dwf.FDwfAnalogInConfigure(hdwf, c_int(0), c_int(1))  # reconfigure + start
        print(f"[ad2] frame {i+1}/{args.frames}: armed, waiting for trigger "
              f"(timeout {args.timeout:.0f} s)...")
        t0 = time.time()
        state = c_byte()
        while True:
            dwf.FDwfAnalogInStatus(hdwf, c_int(1), byref(state))
            if state.value == DwfStateDone.value:
                break
            if time.time() - t0 > args.timeout:
                print("[ad2] TIMEOUT — no trigger fired. Possibilities: trigger "
                      "level too low, probes not connected, board not running, "
                      "or the rail is genuinely stable.")
                dwf.FDwfDeviceCloseAll()
                sys.exit(2)
            time.sleep(0.05)
        elapsed = time.time() - t0
        ts = datetime.datetime.now().isoformat(timespec="seconds").replace(":", "")

        # Pull samples — read both channels by physical index (0,1) and then
        # alias to logical rails (3v3, 5v) using the swap mapping.
        SampleArrayT = c_double * args.samples
        buf0 = SampleArrayT()
        buf1 = SampleArrayT()
        dwf.FDwfAnalogInStatusData(hdwf, c_int(0), buf0, c_int(args.samples))
        dwf.FDwfAnalogInStatusData(hdwf, c_int(1), buf1, c_int(args.samples))
        ch1 = buf1 if args.swap else buf0  # 3V3 rail samples
        ch2 = buf0 if args.swap else buf1  # 5V rail samples

        # Empirically (AD2 defaults regardless of TriggerPositionSet on this
        # firmware) the trigger sits at the buffer midpoint. Treat the first
        # half as pre-trigger and the second half as post-trigger.
        trig_idx = args.samples // 2
        ch1_min = min(ch1)
        ch1_min_idx = list(ch1).index(ch1_min)
        ch1_pre = sum(ch1[i] for i in range(0, max(1, trig_idx-10))) / max(1, trig_idx-10)
        ch1_dip_dv = ch1_pre - ch1_min
        # Recovery: time from min to back >= 90% of pre
        ch1_recover = -1.0
        threshold_recover = ch1_pre * 0.9
        for j in range(ch1_min_idx, args.samples):
            if ch1[j] >= threshold_recover:
                ch1_recover = (j - ch1_min_idx) / args.rate_hz * 1e6
                break

        ch2_min = min(ch2)
        ch2_pre = sum(ch2[i] for i in range(0, max(1, trig_idx-10))) / max(1, trig_idx-10)
        ch2_dip_dv = ch2_pre - ch2_min

        print(f"[ad2] frame {i+1} captured ({elapsed:.1f} s waited)")
        print(f"       CH1 (3V3): pre={ch1_pre:.3f} V  min={ch1_min:.3f} V  "
              f"dip={ch1_dip_dv*1000:.1f} mV  recover_to_90%={ch1_recover:.1f} us")
        print(f"       CH2 (5V):  pre={ch2_pre:.3f} V  min={ch2_min:.3f} V  "
              f"dip={ch2_dip_dv*1000:.1f} mV")
        # Interpretation hint
        if ch2_dip_dv * 1000 < 50:
            five_v_status = "STABLE"
        elif ch2_dip_dv * 1000 < 200:
            five_v_status = "minor dip"
        else:
            five_v_status = "LARGE DIP — USB/PSU cable parasitic dominant"
        if ch1_min > 2.51 + 0.1:
            three_v_status = "stayed above BOD threshold (2.51V) -> dip not enough externally to trigger reset"
        else:
            three_v_status = "BELOW 2.51 V — external 3V3 actually fell below BOD threshold"
        print(f"       => CH2: {five_v_status} | CH1: {three_v_status}")

        # Save CSV
        path = os.path.join(args.outdir, f"capture_{ts}_f{i+1}.csv")
        dt_us = 1e6 / args.rate_hz
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_us_from_buf_start", "ch1_3v3", "ch2_5v"])
            for j in range(args.samples):
                w.writerow([f"{j*dt_us:.2f}", f"{ch1[j]:.4f}", f"{ch2[j]:.4f}"])
        print(f"       saved: {path}")

    dwf.FDwfDeviceCloseAll()
    print("[ad2] done.")


if __name__ == "__main__":
    main()
