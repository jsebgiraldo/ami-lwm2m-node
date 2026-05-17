"""Shared helpers for AMI fleet automation tools.

Centralizes:
  - Toolchain env auto-detection (Zephyr SDK, west venv, esptool)
  - ESP32-C6 native USB COM port enumeration on Windows
  - Deployment ledger CSV append/read (single source of truth)
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass

REQUIRED_PYTHON_DEPS = ("requests", "serial", "paramiko")  # pyserial imports as `serial`

# ── Paths ──────────────────────────────────────────────────────────────
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
LEDGER_PATH = TOOLS_DIR / "deployment_ledger.csv"

# ── Default Edge / OTBR ────────────────────────────────────────────────
# Each mesh has its own TB Edge instance. Resolve via mesh_to_edge() so a tool
# invocation `--mesh r1000` automatically targets the right server unless
# `--host` is overridden explicitly.
MESH_TO_EDGE = {
    "pi4":   ("192.168.1.111", 8090),
    "r1000": ("192.168.8.111", 8090),   # migrated 2026-05-16 from Seeed R1000 (.175) to Pi4 EKH01-DE87 (.111)
}
# Legacy module-level constants (preserved for callers that don't pass mesh)
EDGE_HOST, EDGE_PORT = MESH_TO_EDGE["pi4"]
EDGE_TENANT_USER = "tenant@thingsboard.org"
EDGE_TENANT_PASS = "tenant"
EDGE_PROFILE = "AMI_LwM2M_Node"


def edge_for_mesh(mesh: str) -> tuple[str, int]:
    """Return (host, port) of the TB Edge instance bound to the given mesh."""
    if mesh not in MESH_TO_EDGE:
        raise ValueError(f"Unknown mesh '{mesh}'. Choose: {tuple(MESH_TO_EDGE)}")
    return MESH_TO_EDGE[mesh]

# ── Firmware target ────────────────────────────────────────────────────
DEFAULT_BOARD = "xiao_esp32c6/esp32c6/hpcore"
ESP32C6_VID = "303A"  # Espressif native USB Serial/JTAG
ESP32C6_PID = "1001"

# Build variants: which Thread role the firmware compiles for.
#   med = Minimal End Device (MTD) — default for production fleet
#   ftd = Full Thread Device (router-eligible) — for ~5-10% of nodes
VARIANTS = ("med", "ftd")
DEFAULT_VARIANT = "med"

# Mesh targets: which OTBR / Thread network the firmware will join.
#   pi4   = legacy UNAL-Thread on Pi4 EKH01 (192.168.1.111, channel 25)
#   r1000 = production UNAL-R1000 on Seeed R1000 (192.168.8.175, channel 21)
MESH_TARGETS = ("pi4", "r1000")
DEFAULT_MESH = "r1000"  # production default — pi4 kept as legacy/optional


# ─── Environment detection ─────────────────────────────────────────────
@dataclass
class ToolEnv:
    """Resolved toolchain locations."""
    venv_python: pathlib.Path
    venv_scripts: pathlib.Path  # contains west.exe, esptool.exe
    west_workspace: pathlib.Path  # has .west/ inside
    zephyr_sdk: pathlib.Path

    def env_for_subprocess(self) -> dict:
        """Env vars to pass to west/cmake child processes."""
        env = os.environ.copy()
        env["PATH"] = str(self.venv_scripts) + os.pathsep + env.get("PATH", "")
        env["ZEPHYR_SDK_INSTALL_DIR"] = str(self.zephyr_sdk)
        return env


def _candidates_for_venv() -> list[pathlib.Path]:
    home = pathlib.Path.home()
    return [
        home / "Documents" / "ESP32" / ".venv",
        home / "ESP32" / ".venv",
        home / "zephyrproject" / ".venv",
    ]


def _candidates_for_workspace() -> list[pathlib.Path]:
    home = pathlib.Path.home()
    return [
        home / "Documents" / "ESP32" / "zephyrproject",
        home / "ESP32" / "zephyrproject",
        home / "zephyrproject",
    ]


def _candidates_for_sdk() -> list[pathlib.Path]:
    home = pathlib.Path.home()
    return sorted(home.glob("zephyr-sdk-*"), reverse=True)


def bootstrap_venv() -> None:
    """If the current interpreter is missing critical deps, re-exec under
    the Zephyr venv (which has esptool/west/requests/paramiko/pyserial).

    This makes every entry-point script work whether the user runs it as
    `python tools/X.py` (system Python) or via an absolute path. Skips the
    re-exec if already under the venv, or sets AMI_BOOTSTRAPPED=1 to avoid
    infinite loops.
    """
    if os.environ.get("AMI_BOOTSTRAPPED") == "1":
        return
    # `python -c "..."` cannot be re-exec'd cleanly (the source isn't in argv);
    # nothing to bootstrap here, just let the original -c run.
    if sys.argv and sys.argv[0] == "-c":
        return

    missing: list[str] = []
    for mod in REQUIRED_PYTHON_DEPS:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return

    venv_py = next(
        (p / "Scripts" / "python.exe" for p in _candidates_for_venv()
         if (p / "Scripts" / "python.exe").exists()),
        None,
    )
    if venv_py is None:
        # Cannot bootstrap — surface a useful error
        raise SystemExit(
            f"Missing Python deps {missing}. No Zephyr venv found at "
            + ", ".join(str(p) for p in _candidates_for_venv())
            + ".\nFix:  pip install " + " ".join(
                "pyserial" if m == "serial" else m for m in missing
            )
        )

    print(f"[fleet_common] re-running under {venv_py}", flush=True)
    env = os.environ.copy()
    env["AMI_BOOTSTRAPPED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    res = subprocess.run([str(venv_py), *sys.argv], env=env)
    sys.exit(res.returncode)


def detect_env(verbose: bool = False) -> ToolEnv:
    """Find venv + zephyr workspace + SDK on the user's machine. Raises if missing."""
    venv = next((p for p in _candidates_for_venv() if (p / "Scripts" / "python.exe").exists()), None)
    if not venv:
        raise SystemExit(
            "Couldn't locate a Zephyr Python venv. Expected one of:\n  "
            + "\n  ".join(str(p) for p in _candidates_for_venv())
        )

    ws = next((p for p in _candidates_for_workspace() if (p / ".west").is_dir()), None)
    if not ws:
        raise SystemExit("Couldn't locate the west workspace (.west/ dir).")

    sdks = _candidates_for_sdk()
    if not sdks:
        raise SystemExit("Couldn't locate any zephyr-sdk-* under your home directory.")
    sdk = sdks[0]

    env = ToolEnv(
        venv_python=venv / "Scripts" / "python.exe",
        venv_scripts=venv / "Scripts",
        west_workspace=ws,
        zephyr_sdk=sdk,
    )
    if verbose:
        print(f"[env] python   = {env.venv_python}")
        print(f"[env] west ws  = {env.west_workspace}")
        print(f"[env] sdk      = {env.zephyr_sdk}")
    return env


# ─── COM port enumeration (Windows-first, falls back to pyserial) ──────
@dataclass
class ESP32Port:
    com: str           # e.g. "COM8"
    pnp_id: str        # raw PNPDeviceID with VID/PID/serial
    serial_id: str     # extracted unique substring (best-effort)


def list_esp32c6_ports() -> list[ESP32Port]:
    """Return ESP32-C6 native USB Serial/JTAG ports currently enumerated."""
    out: list[ESP32Port] = []
    try:
        import serial.tools.list_ports as lp  # pyserial
    except Exception:
        return out
    needle = f"VID:PID={ESP32C6_VID}:{ESP32C6_PID}".upper()
    for p in lp.comports():
        hwid = (p.hwid or "").upper()
        if needle in hwid:
            # extract a serial-ish id (last token after colon/backslash)
            sid = ""
            for sep in (":", "\\", " "):
                if sep in hwid:
                    sid = hwid.split(sep)[-1]
                    break
            out.append(ESP32Port(com=p.device, pnp_id=hwid, serial_id=sid))
    out.sort(key=lambda x: int(x.com.replace("COM", "")) if x.com.startswith("COM") else 0)
    return out


# ─── MAC reading ────────────────────────────────────────────────────────
def _esptool_cmd(env: ToolEnv, *extra: str) -> list[str]:
    esptool = env.venv_scripts / "esptool.exe"
    if esptool.exists():
        return [str(esptool), *extra]
    return [str(env.venv_python), "-m", "esptool", *extra]


def read_mac(env: ToolEnv, com: str, timeout_s: int = 30) -> str:
    """Run esptool chip-id on given COM, return base MAC (e.g. '10:51:db:1c:15:58')."""
    cmd = _esptool_cmd(env, "--port", com, "chip-id")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, env=env.env_for_subprocess())
    if res.returncode != 0:
        raise RuntimeError(f"esptool chip-id failed:\n{res.stdout}\n{res.stderr}")
    for line in res.stdout.splitlines():
        s = line.strip()
        if s.upper().startswith("BASE MAC:"):
            return s.split(":", 1)[1].strip()
    raise RuntimeError(f"BASE MAC not found in esptool output:\n{res.stdout}")


def erase_flash(env: ToolEnv, com: str, timeout_s: int = 60) -> None:
    """Erase the entire flash. Recommended once before flashing a new (factory-fresh)
    or repurposed board to avoid stale partition/bootloader state preventing boot."""
    cmd = _esptool_cmd(env, "--port", com, "erase-flash")
    print(f"[erase] {com}  (esptool erase-flash)")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, env=env.env_for_subprocess())
    if res.returncode != 0:
        raise RuntimeError(f"esptool erase-flash failed:\n{res.stdout}\n{res.stderr}")


def hard_reset(com: str, label: str = "reset") -> None:
    """Best-effort cold-boot of an ESP32-C6 native-USB board without operator
    intervention. The simple DTR/RTS pulse alone is not enough on native USB
    because RTS already mapped to EN was toggled by west flash; OpenThread
    NVS settings persistence sometimes still requires a fresh boot cycle.

    What actually works: drive the chip into ROM bootloader and back out via
    esptool's connect/run sequence. That is what an unplug/replug effectively
    does (cold start through ROM). We invoke `esptool chip_id` with the
    explicit `--before default_reset --after hard_reset` to force exactly
    this transition.
    """
    import time as _time
    env = detect_env()
    cmd = _esptool_cmd(env, "--port", com,
                       "--before", "default-reset",
                       "--after", "hard-reset",
                       "--connect-attempts", "3",
                       "chip-id")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                             env=env.env_for_subprocess())
        if res.returncode == 0:
            print(f"[{label}] cold-boot via bootloader OK on {com}")
            return
        print(f"[{label}] esptool reset rc={res.returncode}; falling back to DTR/RTS pulse")
    except Exception as e:
        print(f"[{label}] esptool reset failed ({e}); falling back to DTR/RTS pulse")

    # Fallback: bare DTR/RTS pulse via pyserial
    try:
        import serial as _ser
        s = _ser.Serial(); s.port = com; s.baudrate = 115200; s.timeout = 0.1
        s.dtr = False; s.rts = False
        s.open(); s.dtr = False; s.rts = False
        s.rts = True; _time.sleep(0.15); s.rts = False; _time.sleep(0.05)
        s.close()
        print(f"[{label}] DTR/RTS pulsed on {com}")
    except Exception as e:
        print(f"[{label}] DTR/RTS fallback skipped: {e}")


def mac_to_endpoint(mac: str) -> str:
    parts = [p for p in mac.replace("-", ":").split(":") if p]
    if len(parts) < 2:
        raise ValueError(f"Invalid MAC '{mac}'")
    return f"ami-esp32c6-{parts[-2].lower()}{parts[-1].lower()}"


# ─── Firmware artifact discovery ───────────────────────────────────────
def build_dir(env: ToolEnv, variant: str = DEFAULT_VARIANT,
              mesh: str = DEFAULT_MESH) -> pathlib.Path:
    """West build directory per (variant, mesh) tuple.

    Each combination has its own dir: build_<variant>_<mesh>
    e.g. build_med_r1000, build_ftd_r1000, build_med_pi4, build_ftd_pi4.
    No special case for the default mesh — keeps the artifact layout
    explicit and avoids silent binary swaps when the default changes.
    """
    if variant not in VARIANTS:
        raise ValueError(f"Unknown variant '{variant}'. Choose: {VARIANTS}")
    if mesh not in MESH_TARGETS:
        raise ValueError(f"Unknown mesh '{mesh}'. Choose: {MESH_TARGETS}")
    return env.west_workspace / f"build_{variant}_{mesh}"


def firmware_bin(env: ToolEnv, variant: str = DEFAULT_VARIANT,
                 mesh: str = DEFAULT_MESH) -> pathlib.Path:
    """Path to the built firmware binary for the given (variant, mesh)."""
    return build_dir(env, variant, mesh) / "zephyr" / "zephyr.bin"


def firmware_hash(env: ToolEnv, variant: str = DEFAULT_VARIANT,
                  mesh: str = DEFAULT_MESH) -> str:
    p = firmware_bin(env, variant, mesh)
    if not p.exists():
        return ""
    h = hashlib.sha256(p.read_bytes()).hexdigest()[:12]
    return h


def overlay_path(variant: str) -> pathlib.Path:
    """Overlay file for a build variant (med/ftd)."""
    return REPO_ROOT / "overlays" / f"{variant}.conf"


def mesh_overlay_path(mesh: str) -> pathlib.Path:
    """Overlay file for a mesh target (pi4/r1000)."""
    if mesh not in MESH_TARGETS:
        raise ValueError(f"Unknown mesh '{mesh}'. Choose: {MESH_TARGETS}")
    return REPO_ROOT / "overlays" / f"{mesh}.conf"


def composed_overlay_arg(variant: str, mesh: str) -> str:
    """Return the value to pass as -DEXTRA_CONF_FILE for the (variant, mesh) combo.

    Zephyr supports multiple overlay files separated by `;` on Windows /
    space on Unix. We use `;` since this tool runs on Windows primarily.
    """
    paths = [overlay_path(variant), mesh_overlay_path(mesh)]
    return ";".join(str(p) for p in paths)


# ─── Ledger ─────────────────────────────────────────────────────────────
# `mesh` records which Thread network the firmware was built for; the same
# endpoint can exist in multiple TB Edge instances if a node is migrated.
# `tb_host` is the IP of the TB Edge it was provisioned against.
LEDGER_FIELDS = [
    "ts_iso", "endpoint", "mac", "com", "board", "variant", "mesh",
    "fw_sha", "tb_host", "status", "tb_device_id", "notes",
]


def ledger_append(row: dict) -> None:
    LEDGER_PATH.parent.mkdir(exist_ok=True)
    new_file = not LEDGER_PATH.exists()
    # Forward-compatible: if old file exists without 'variant' column, append rows
    # leaving variant blank (csv.DictWriter ignores unknown fields by default).
    with LEDGER_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LEDGER_FIELDS})


def ledger_read() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    with LEDGER_PATH.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
