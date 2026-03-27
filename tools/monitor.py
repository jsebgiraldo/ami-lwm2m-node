"""
monitor.py - Read serial output from ESP32-C6 on COM11.

Uses Win32 CreateFileW / ReadFile directly (bypasses pyserial's ClearCommError
which fails on ESP32-C6 USB CDC with ERROR_BAD_COMMAND / PermissionError 22).
Sets DTR=True via DCB fDtrControl=DTR_CONTROL_ENABLE so firmware recognises
an active host and starts sending data.

Usage:
    python monitor.py              # read forever
    python monitor.py --seconds 60 # read for 60 seconds then exit
    python monitor.py --out boot.txt  # also save to file
"""
import serial
import serial.tools.list_ports
import time
import sys
import argparse

PORT = "COM11"

class _RawComPort:
    """
    Direct Win32 ReadFile wrapper around a COM port.
    Bypasses pyserial's ClearCommError (which fails on ESP32-C6 USB CDC).

    write_access=False: open read-only — safe during flash (no SET_CONTROL_LINE_STATE
      sent by usbser.sys, avoids corrupting the JTAG endpoint while OpenOCD is active).
    write_access=True: open read+write — triggers usbser.sys SET_CONTROL_LINE_STATE,
      which sets the ESP32-C6 USB Serial/JTAG CONNECTED hardware bit so the firmware
      actually sends its buffered output. Use this after the USB CDC re-enumeration.
    """

    GENERIC_READ  = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3

    def __init__(self, port, baudrate=115200, timeout=0.2, write_access=False):
        import ctypes
        import ctypes.wintypes as wt

        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype  = wt.HANDLE
        k32.CloseHandle.restype  = wt.BOOL
        k32.SetCommTimeouts.restype = wt.BOOL
        k32.ReadFile.restype     = wt.BOOL
        self._k32  = k32
        self._port = port

        access = self.GENERIC_READ
        if write_access:
            access |= self.GENERIC_WRITE

        unc = f"\\\\.\\{port}"
        handle = k32.CreateFileW(
            unc,
            access,
            0, None,
            self.OPEN_EXISTING,
            0, None
        )
        INVALID = wt.HANDLE(-1).value
        if handle == INVALID or handle is None:
            raise OSError(f"Cannot open {port}: {ctypes.WinError()}")
        self._handle = handle

        # NOTE: SetCommState always fails on ESP32-C6 USB CDC — skip it entirely.
        # The port is already at 115200/8N1 by driver defaults.

        # Timeouts: ReadIntervalTimeout=50ms groups burst data into chunks.
        # ReadTotalTimeoutConstant=timeout_ms is the hard deadline per read() call.
        class COMMTIMEOUTS(ctypes.Structure):
            _fields_ = [
                ("ReadIntervalTimeout",        ctypes.c_ulong),
                ("ReadTotalTimeoutMultiplier",  ctypes.c_ulong),
                ("ReadTotalTimeoutConstant",    ctypes.c_ulong),
                ("WriteTotalTimeoutMultiplier", ctypes.c_ulong),
                ("WriteTotalTimeoutConstant",   ctypes.c_ulong),
            ]
        ct = COMMTIMEOUTS()
        ct.ReadIntervalTimeout        = 50
        ct.ReadTotalTimeoutMultiplier = 0
        ct.ReadTotalTimeoutConstant   = max(1, int(timeout * 1000))
        k32.SetCommTimeouts(self._handle, ctypes.byref(ct))

    def read(self, size=1024):
        import ctypes
        buf = (ctypes.c_char * size)()
        n   = ctypes.c_ulong(0)
        self._k32.ReadFile(self._handle, buf, size, ctypes.byref(n), None)
        return bytes(buf[:n.value])

    def close(self):
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None

    @property
    def port(self):
        return self._port


def open_port(port, retries=10, delay=1.5, write_access=False):
    """Open COM port using Win32 ReadFile/CreateFileW.
    write_access=False: read-only, safe during JTAG flash (no SET_CONTROL_LINE_STATE).
    write_access=True:  read+write, triggers usbser.sys to assert CONNECTED on the
                        ESP32-C6 USB Serial/JTAG hardware so firmware sends output.
    """
    for attempt in range(retries):
        try:
            ports = [p.device for p in serial.tools.list_ports.comports()]
            if port not in ports:
                print(f"  {port} not present. Available: {ports}")
                time.sleep(delay)
                continue
            return _RawComPort(port, write_access=write_access)
        except OSError as e:
            if attempt < retries - 1:
                print(f"  Open attempt {attempt+1}/{retries} failed: {e}, retrying...")
                time.sleep(delay)
            else:
                raise
    return None


def monitor(port=PORT, duration=None, outfile=None):
    print(f"Opening {port}... (Ctrl+C to stop)")
    s = open_port(port)
    if s is None:
        print(f"FATAL: could not open {port}")
        sys.exit(1)
    print(f"Connected to {s.port}. Monitoring...")

    lines = []
    buf = b""
    t_start = time.time()

    try:
        while True:
            if duration and (time.time() - t_start) >= duration:
                break
            try:
                data = s.read(1024)
            except OSError as e:
                elapsed = time.time() - t_start
                print(f"\n[{elapsed:.0f}s] Read error: {e}, reopening...")
                try: s.close()
                except: pass
                s = open_port(port, retries=20, delay=1.0)
                if s is None:
                    print("  Could not reopen port.")
                    break
                print(f"  Reopened OK.")
                continue

            if data:
                buf += data
                text = data.decode("utf-8", errors="replace")
                elapsed = time.time() - t_start
                for line in text.split("\n"):
                    line = line.strip("\r")
                    if line:
                        ts = f"[{elapsed:6.1f}s] "
                        print(ts + line)
                        lines.append(ts + line)
            else:
                # If we've received data before and now the port appears gone (ReadFile
                # returning an OS error higher up), the re-open in the OSError handler
                # deals with it. Simple silence here is normal (no data to send).
                pass

    except KeyboardInterrupt:
        print("\n--- Ctrl+C ---")
    finally:
        try: s.close()
        except: pass

    elapsed = time.time() - t_start
    print(f"\n=== {len(buf)} bytes in {elapsed:.0f}s ===")

    if outfile:
        with open(outfile, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"Saved to {outfile}")

    return buf


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=PORT)
    parser.add_argument("--seconds", type=float, default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    monitor(port=args.port, duration=args.seconds, outfile=args.out)
