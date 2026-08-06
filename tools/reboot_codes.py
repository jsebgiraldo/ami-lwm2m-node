"""Canonical decode tables for Object 33000 reboot forensics.

Single source of truth for the RID 37 reboot-path map and the RID 21 hwinfo
reset-cause bitmap. Four tools used to carry private copies of these dicts and
all four silently drifted: they stopped at code 11 and so decoded every
hardware-watchdog reboot added in v0.7.18 as "other".

Firmware side of the contract:
  - codes 1-10   src/main.c, ami_reboot_reason_to_code() string table
  - codes 11-16  stamped at their sys_reboot() site (see the CANONICAL RID 37
                 code map comment in src/main.c, just below that table)
Keep this file in step with that comment.
"""

# ── RID 37 — which firmware path rebooted the node ────────────────────────
#
# 0 is the important one to read correctly: it means the reset did NOT pass
# through any firmware path. Before v0.7.18 it also swallowed all four
# hardware-watchdog paths and the OTA reboot, so a hung node and a dead power
# supply landed in the same bucket — that ambiguity is why the 2026-08-05
# fleet census could not attribute 27 of 35 live nodes.
REBOOT_CODE = {
    0:  "(power-on / brownout / external)",
    1:  "boot-watchdog",
    2:  "mesh-alone-watchdog",
    3:  "conn-mon-no-first-tick",
    4:  "conn-mon-WEDGED",
    5:  "max-recover-attempts",
    6:  "lwm2m-device-reboot",
    7:  "shell-command",
    8:  "ip6-enable-failed",
    9:  "thread-enable-failed",
    10: "dns-sd-boot-fail",
    11: "PANIC",
    12: "hw-wdog-boot-grace",     # never REGISTERed within the hard cap
    13: "hw-wdog-delivery-stall",  # registered but ZERO observers
    14: "hw-wdog-silence",         # server stopped ACKing REG_UPDATE
    15: "hw-wdog-channel",         # a task_wdt channel went mute
    16: "ota-apply",               # reboot into the freshly staged image
    99: "other",
}

# Codes that mean "the node hung and the watchdog shot it", as opposed to a
# planned reboot or a power event. Useful for fleet-health aggregation.
HUNG_CODES = frozenset({12, 13, 14, 15})

# ── RID 21 — hwinfo_get_reset_cause() bitmap ──────────────────────────────
#
# NOTE on BROWNOUT (bit 2): the ESP32-C6 detector is left at its default
# LVL_7 (~2.51 V) because the Kconfig symbol is rejected from prj.conf — see
# prj.conf "Power / Brownout resilience". The chip needs 3.0 V, so a sag to
# 2.8 V corrupts it without ever tripping the detector. Absence of BROWNOUT
# here is NOT evidence that the supply is healthy.
RESET_CAUSE = {
    0:   "unknown",
    1:   "PIN",
    2:   "SOFTWARE",
    4:   "BROWNOUT",
    8:   "POR",
    16:  "WATCHDOG",
    32:  "DEBUG",
    128: "LOW_POWER_WAKE",
    256: "CPU_LOCKUP",
}


def decode_reboot_code(value):
    """Human label for a RID 37 value. Accepts None/str/int."""
    if value is None:
        return "(no data)"
    try:
        return REBOOT_CODE.get(int(value), f"code {int(value)}")
    except (TypeError, ValueError):
        return f"code {value!r}"


def decode_reset_cause(value):
    """Human label for a RID 21 bitmap. Reports every bit set, not just one —
    a reset can legitimately carry more than one cause."""
    if value is None:
        return "(no data)"
    try:
        v = int(value)
    except (TypeError, ValueError):
        return f"raw {value!r}"
    if v < 0:
        return "hwinfo read failed"
    if v == 0:
        return "unknown"
    bits = [name for bit, name in sorted(RESET_CAUSE.items())
            if bit and (v & bit)]
    return "|".join(bits) if bits else f"raw {v}"
