# LAB OTBR Bring-Up — Local OpenThread Border Router on Windows 11 (WSL2 + ZBDongle-E)

> **Goal:** stand up a self-contained benchtop Thread network on THIS Windows PC so a
> XIAO ESP32-C6 AMI node can join it and we can reach its CoAP `/diag` + `/ami`
> endpoints over IPv6 while a FNIRSI FNB-C2 watches the node's power rail. This is a
> **local test bed** — separate from the production Pi4/R1000 fleet. No ThingsBoard,
> no SSH OTBR; the border router is a **docker container inside WSL2**, driven with
> `docker exec otbr ot-ctl ...`.

**Radio-co-processor (RCP):** SONOFF ZBDongle-E (Silabs **EFR32MG21**, CP210x bridge)
must run **`ot-rcp`** firmware — it ships from the factory with EmberZNet **EZSP**
(Zigbee) firmware, which OTBR cannot use. Step 1 reflashes it.

## Hardware map (this session)

| Device | Role | Windows port | USB VID:PID |
|---|---|---|---|
| SONOFF ZBDongle-E (EFR32MG21) | Thread **RCP** for OTBR | **COM58** | `10c4:ea60` (CP210x) |
| XIAO ESP32-C6 (`98:A3:16:61:3B:B0`) | AMI **node under test** | **COM53** (USB-Serial-JTAG) | — |
| FNIRSI FNB-C2 | USB power meter | COM8 / HID `MI_02` | `2E3C:5558` |

> COM4 and COM85 belong to **other projects** — never touch them.

## What "up" means (acceptance)

1. `docker exec otbr ot-ctl state` → **`leader`**
2. `docker exec otbr ot-ctl br omrprefix` → a **`fd..::/64`** OMR prefix
3. `docker exec otbr ot-ctl netdata show` → that OMR prefix is listed under **Prefixes**
4. The ESP32-C6 node, built with the LAB dataset, attaches and answers
   `python tools/diag_get.py --local --addr <node-OMR>` with a 2.05 JSON snapshot.

---

## Prerequisites (confirmed present on this PC)

- **WSL2** with **Ubuntu-24.04** (`wsl -l -v`)
- **usbipd-win** (`usbipd --version`) — [install](https://github.com/dorssel/usbipd-win) if missing: `winget install usbipd`
- **Docker** ≥ 29 inside WSL2 (`wsl -d Ubuntu-24.04 -- docker version`)
- **Python** with `pyserial` (venv: `C:/Users/jsgir/Documents/ESP32/.venv/Scripts/python.exe`) for the flasher
- The **`universal-silabs-flasher`** pip package (installed in Step 1)

Helper scripts written for this runbook:
- `tools/lab_otbr_up.ps1` — Windows-side usbipd attach + `health` check
- `tools/lab_thread_creds.py` — pull the OTBR dataset → emit node firmware creds

---

## Step 1 — Flash the ZBDongle-E with `ot-rcp` firmware

The `ot-rcp` builds for the ZBDongle-E come from the community
[**darkxst/silabs-firmware-builder**](https://github.com/darkxst/silabs-firmware-builder/tree/main/firmware_builds/zbdonglee)
repo (a fork of [NabuCasa/silabs-firmware-builder](https://github.com/NabuCasa/silabs-firmware-builder)).
Pick the **460800-baud** variant so it matches OTBR's default UART baud, e.g.
`ot-rcp-v2.4.5.0-zbdonglee-460800.gbl`.

> Flash from **Windows against COM58** — simplest, and it avoids the usbipd dance for
> the flash step (the CP210x driver is already installed). The
> [`universal-silabs-flasher`](https://github.com/NabuCasa/universal-silabs-flasher)
> is pure-Python + pyserial and runs fine on Windows.

```powershell
# 1a. Install the flasher into the project venv
& C:/Users/jsgir/Documents/ESP32/.venv/Scripts/python.exe -m pip install universal-silabs-flasher

# 1b. Download the ot-rcp gbl (browser) from darkxst/silabs-firmware-builder ->
#     firmware_builds/zbdonglee/ot-rcp-v2.4.5.0-zbdonglee-460800.gbl
#     Save it next to this repo, e.g. C:\Users\jsgir\Downloads\ot-rcp-zbdonglee-460800.gbl

# pip install drops the console script here:
$FLASHER = "C:/Users/jsgir/Documents/ESP32/.venv/Scripts/universal-silabs-flasher.exe"

# 1c. Probe — confirms the flasher can talk to the dongle and reports the
#     currently-running app (you'll likely see EZSP / EmberZNet = Zigbee).
& $FLASHER --device COM58 probe

# 1d. Flash ot-rcp. The Sonoff dongles enter the Gecko bootloader over RTS/DTR.
& $FLASHER --device COM58 --bootloader-reset rts_dtr `
    flash --firmware C:\Users\jsgir\Downloads\ot-rcp-zbdonglee-460800.gbl
```

**CP210x bootloader entry.** The flasher toggles RTS/DTR to reboot the EFR32 into its
Gecko serial bootloader (which runs at 115200 for the XMODEM transfer); the `460800`
in the filename is the baud the *ot-rcp application* runs at afterward — that is the
baud OTBR must use in `RADIO_URL`. If `probe`/reset fails (dongle wedged in a bad app),
force the bootloader manually: **unplug, hold the tiny BOOT button** under the case,
plug back in, release, then re-run `flash`.

> **Alternative (flash from inside WSL):** attach the dongle first (Step 2), then
> `pip install universal-silabs-flasher` in WSL and use `--device /dev/ttyUSB0`. Only
> do this if Windows-side flashing misbehaves; it is otherwise an extra moving part.

Verify after flashing: `probe` should now report an **`ot-rcp` / spinel** application
instead of EZSP.

---

## Step 2 — Pass COM58's USB device into WSL2 with usbipd

The OTBR runs inside WSL2, so the RCP's USB device must be attached there. `bind` is a
one-time (persistent) admin action; `attach` is **per-session** and must be repeated
after every replug or `wsl --shutdown`.

```powershell
# 2a. Find the busid (look for VID:PID 10c4:ea60)
usbipd list

# 2b. Bind — ONE TIME, from an ELEVATED PowerShell (persists across reboots)
usbipd bind --busid <BUSID>          # e.g. 2-4

# 2c. Attach into WSL2 (re-run after any replug / wsl --shutdown)
usbipd attach --wsl --busid <BUSID> --distribution Ubuntu-24.04
```

Or use the helper (does list → bind → attach and prints the `/dev/ttyUSB*`):

```powershell
./tools/lab_otbr_up.ps1 -Action attach      # run the FIRST time from an elevated shell
```

Confirm inside WSL — the RCP should be **`/dev/ttyUSB0`**:

```powershell
wsl -d Ubuntu-24.04 -- bash -lc "ls -l /dev/ttyUSB*; dmesg | grep -i cp210x | tail"
```

> **Auto re-attach:** `usbipd attach --wsl --busid <BUSID> --auto-attach` keeps a
> foreground watcher that re-attaches on replug — handy during a long soak.

---

## Step 3 — Run the OTBR container and form the Thread network

Inside WSL2, run the prebuilt `openthread/otbr` image with the RADIO_URL pointing at
the RCP at **460800** baud. Use `--network host` so the border-router's OMR route and
RA live in WSL's own network namespace (that is what makes the node reachable from
WSL — see the networking gotchas). `eth0` is WSL's infra/backbone interface.

```bash
# Run all of this INSIDE WSL:  wsl -d Ubuntu-24.04
sudo modprobe ip6table_filter 2>/dev/null || true    # some kernels need this for BR

docker run --name otbr -d --restart unless-stopped \
  --privileged \
  --network host \
  --volume /dev/ttyUSB0:/dev/ttyUSB0 \
  --sysctl "net.ipv6.conf.all.disable_ipv6=0 net.ipv4.conf.all.forwarding=1 net.ipv6.conf.all.forwarding=1" \
  openthread/otbr \
  --radio-url 'spinel+hdlc+uart:///dev/ttyUSB0?uart-baudrate=460800' \
  --backbone-interface eth0
```

Wait a few seconds, then form the network via `ot-ctl`. We pin **channel 25** and
**PAN 0xEFEB** (matches the canonical snapshot schema); the network key here is an
explicit, reproducible lab key — change it if you like, it is bench-only.

```bash
docker exec -it otbr ot-ctl <<'EOF'
dataset init new
dataset channel 25
dataset panid 0xEFEB
dataset networkkey 00112233445566778899aabbccddeeff
dataset commit active
ifconfig up
thread start
EOF

# Give it ~10 s to become leader, then enable SRP (service discovery for the node)
sleep 10
docker exec otbr ot-ctl state            # -> leader
docker exec otbr ot-ctl srp server enable
docker exec otbr ot-ctl br omrprefix     # -> fd..::/64  (routable OMR)
docker exec otbr ot-ctl netdata show     # OMR prefix must appear under "Prefixes"
```

**RADIO_URL / baud:** `spinel+hdlc+uart:///dev/ttyUSB0?uart-baudrate=460800` — the
`?uart-baudrate=460800` **must** match the flashed `ot-rcp` build. A mismatch shows up
as `RadioSpinelNoResponse` in `docker logs otbr`. (References:
[OpenThread Docker guide](https://openthread.io/guides/border-router/docker),
[Silabs AN1256](https://www.silabs.com/documents/public/application-notes/an1256-using-sl-rcp-with-openthread-border-router.pdf).)

**Border routing / SRP — why they matter here:** `--backbone-interface eth0` + a
started Thread network makes otbr-agent publish the **OMR prefix** and advertise a
route on WSL's `eth0`; that on-mesh-routable prefix is exactly what lets our CoAP
`/diag` traffic reach the node from WSL. `srp server enable` lets the node register
its service/AAAA records (and is what the node's SRP-based LwM2M discovery expects on
the production networks — harmless and useful to leave on here).

---

## Step 4 — Put THIS network's creds into the ESP32-C6 node firmware

The AMI firmware does **not** join via a commissioner — it compiles a hardcoded Thread
**operational-dataset TLV blob** into the image (see `src/main.c` ::
`apply_otbr_dataset()`, the `otbr_tlvs[]` arrays under `CONFIG_AMI_MESH_PI4` /
`CONFIG_AMI_MESH_R1000`). To join the lab network you regenerate that blob from the
OTBR's live dataset. `tools/lab_thread_creds.py` does it end-to-end.

```powershell
# From Windows PowerShell — tunnel ot-ctl through WSL. Prints the C block +
# overlays/lab.conf + the Kconfig snippet, and (optionally) writes the overlay.
python tools\lab_thread_creds.py `
    --exec "wsl -d Ubuntu-24.04 docker exec otbr ot-ctl" --write-overlay
```

(From inside WSL it is simply `python3 tools/lab_thread_creds.py --write-overlay`,
since the default source is `docker exec otbr ot-ctl dataset active -x`. You can also
paste a captured blob with `--hex ...` or `--from-file dataset.hex`.)

It emits three paste-ready artifacts:

**A. C `otbr_tlvs[]` block** → add a new branch to `src/main.c` ::
`apply_otbr_dataset()`, inserted into the `#if / #elif` chain **above the `#else`**:

```c
#elif defined(CONFIG_AMI_MESH_LAB)
    static const uint8_t otbr_tlvs[] = {
        /* LAB benchtop OTBR ... Ch25, PAN 0xefeb ... */
        0x0e, 0x08, /* ... generated bytes ... */ 0x19,
    };
    const char *mesh_label = "LAB bench (...)";
    const char *mesh_local_str = "fd..::/64";
```

**B. `overlays/lab.conf`** (cosmetic Kconfig — startup banner only; the real dataset is
the TLV blob above). Mirrors `overlays/pi4.conf` / `overlays/r1000.conf`:

```conf
CONFIG_AMI_MESH_LAB=y
CONFIG_OPENTHREAD_CHANNEL=25
CONFIG_OPENTHREAD_PANID=61419          # 0xEFEB
CONFIG_OPENTHREAD_NETWORK_NAME="..."
CONFIG_OPENTHREAD_XPANID=".."
CONFIG_OPENTHREAD_NETWORKKEY=".."
```

**C. A new option in the `choice AMI_MESH`** block in `Kconfig` (~line 571):

```kconfig
config AMI_MESH_LAB
    bool "LAB benchtop OTBR (WSL2 docker + ZBDongle-E RCP)"
    help
      Local benchtop test network. Dataset regenerated by
      tools/lab_thread_creds.py. NOT for production nodes.
```

Then build + flash the XIAO ESP32-C6 (COM53) against the lab mesh:

```powershell
# Build with the med role variant + the lab mesh overlay
python tools\build_firmware.py --variant med --mesh lab
# (equivalently: west build ... -- -DEXTRA_CONF_FILE="overlays/med.conf;overlays/lab.conf")

# Flash the node over its native USB-Serial-JTAG on COM53 (orchestrator runs this).
```

> **Quick-and-dirty alternative (no Kconfig/overlay edits):** overwrite the existing
> `CONFIG_AMI_MESH_R1000` `otbr_tlvs[]` bytes in `src/main.c` with the generated block
> and build `--mesh r1000`. Fast for a one-off bench build, but it *clobbers the
> production r1000 creds in the source tree* — do **not** commit that to `master`.
> Prefer the clean `AMI_MESH_LAB` branch above for anything that lives more than an hour.

---

## Step 5 — Health check + reach the node's OMR over IPv6

```powershell
# One-shot "is it up?" from Windows (wraps the docker exec checks):
./tools/lab_otbr_up.ps1 -Action health
```

Manual equivalents:

```bash
docker exec otbr ot-ctl state           # leader
docker exec otbr ot-ctl netdata show    # OMR prefix under Prefixes; routes present
docker exec otbr ot-ctl childtable      # the ESP32-C6 shows up once it attaches
docker exec otbr ot-ctl childip         # -> the child's mesh-local addresses
```

**Reach the node.** Get the node's OMR address (starts with the OMR prefix, `fd..`):
read it from the node's serial banner, or from `ot-ctl childip` / the node's `/diag`.
Then probe `/diag` (fw/role/uptime/resets) and `/ami` (live OBIS) with the repo's raw
CoAP client:

```bash
# Run from INSIDE WSL (it has the OMR route natively — see gotchas):
python3 tools/diag_get.py --local --addr <node-OMR>
python3 tools/diag_get.py --local --ami --addr <node-OMR>
# Or a plain ICMPv6 ping from WSL:
ping6 -c3 <node-OMR>
```

`diag_get.py` sends a CoAP **CON** GET to `[<omr>]:5685/diag` with retransmit — reuse
its logic; the lab e2e monitor targets the same endpoint.

---

## Teardown

```bash
# Inside WSL — stop the mesh + container
docker exec otbr ot-ctl thread stop
docker stop otbr && docker rm otbr
```

```powershell
# Windows — hand COM58 back to Windows (or just replug)
./tools/lab_otbr_up.ps1 -Action detach
# equivalently: usbipd detach --busid <BUSID>
```

To wipe network state for a clean re-form next time, delete the container's data
volume before re-running (`docker rm otbr` already drops the ephemeral state used
above; add `--volume /var/lib/otbr:/data` if you want persistence, then `rm -rf` it).

---

## Windows / WSL2 / Docker gotchas (read this — it is where the time goes)

1. **usbipd attach is NOT persistent.** After any dongle **replug** or `wsl
   --shutdown`, `/dev/ttyUSB0` disappears and the container loses the radio
   (`RadioSpinelNoResponse` in `docker logs otbr`). Re-run
   `usbipd attach --wsl --busid <BUSID>` (or `-Action attach`), then
   `docker restart otbr`. `bind` persists; `attach` does not.

2. **USB device number can renumber.** After a replug the busid or the WSL
   `/dev/ttyUSBx` index can change (cf. the fleet's ACM-renumbering pain on the Pi4
   OTBR). Always re-check `usbipd list` and `ls /dev/ttyUSB*`; if it came up as
   `ttyUSB1`, update the container's `--volume`/`RADIO_URL` or just re-create it.

3. **IPv6 reachability from the *Windows* host is the hard part.** With `--network
   host` in WSL2, the OMR route lives in WSL's Linux namespace — so probes **from
   inside WSL** (`diag_get.py --local`, `ping6`) Just Work. Reaching the node's OMR
   from a **Windows** process (e.g. the venv monitor) is not automatic under WSL2's
   default **NAT** networking. Two options:
   - **Run the CoAP probes / e2e monitor from inside WSL** (recommended — zero routing
     fuss), or drive them through `wsl -d Ubuntu-24.04 -- python3 tools/diag_get.py ...`.
   - **Enable WSL2 mirrored networking** so WSL shares the host's interfaces and IPv6:
     put this in `%UserProfile%\.wslconfig`, then `wsl --shutdown`:
     ```ini
     [wsl2]
     networkingMode=mirrored
     ```
     Mirrored mode gives native IPv6 into WSL but is finicky — there are open reports
     of [IPv6-into-WSL](https://github.com/microsoft/WSL/issues/11679) and
     [Docker-container IPv6](https://github.com/microsoft/WSL/issues/10663) breakage,
     and it can **conflict with `docker --network host`** port binding. If mirrored
     mode fights Docker, fall back to running probes inside WSL.
   - **Last resort static route** (best-effort): read the OMR prefix
     (`ot-ctl br omrprefix`) and WSL's `eth0` IPv6, then on Windows
     `netsh interface ipv6 add route <omr>/64 "vEthernet (WSL)" <wsl-eth0-ipv6>`.
     Fragile across WSL restarts; prefer the in-WSL path.

4. **`--network host` semantics differ in WSL2.** "host" is the *WSL VM's* network, not
   Windows. That is fine here — we want the BR to manage WSL's `eth0`. Do **not** expect
   the container to bind Windows ports. If you also run **Docker Desktop** (vs. Docker
   Engine inside the distro), host networking + mirrored mode is known-flaky; the Docker
   Engine-in-distro path used above is the reliable one.

5. **Baud must match end-to-end.** Flashed `ot-rcp` build baud == `RADIO_URL`
   `uart-baudrate`. We standardize on **460800** (OTBR's default and a stable rate for
   the EFR32MG21 over CP210x). 115200 also works but is slower under DTLS/large frames.

6. **Bootloader entry can need the physical BOOT button.** If `universal-silabs-flasher
   probe` can't reach the app to trigger an RTS/DTR bootloader reboot (dongle stuck in a
   half-flashed state), unplug → hold BOOT → plug → release → re-run `flash`.

7. **IPv6 forwarding sysctls.** The `--sysctl "... .forwarding=1"` flags are required for
   border routing inside the container; without them the OMR prefix forms but traffic
   won't route. Some minimal WSL kernels also need `modprobe ip6table_filter`.

8. **Don't flash the dongle while it's attached to WSL.** COM58 and `/dev/ttyUSB0` are
   the same physical device; a device attached to WSL is invisible to Windows. `detach`
   first if you want to re-flash from Windows.

---

## Command quick-reference

| Want | Command |
|---|---|
| Find dongle busid | `usbipd list` (VID:PID `10c4:ea60`) |
| Bind (one-time, admin) | `usbipd bind --busid <BUSID>` |
| Attach to WSL | `usbipd attach --wsl --busid <BUSID> --distribution Ubuntu-24.04` |
| Flash ot-rcp (Windows) | `universal-silabs-flasher --device COM58 --bootloader-reset rts_dtr flash --firmware ot-rcp-...-460800.gbl` |
| Start OTBR | `docker run --name otbr -d --privileged --network host --volume /dev/ttyUSB0:/dev/ttyUSB0 --sysctl "..." openthread/otbr --radio-url 'spinel+hdlc+uart:///dev/ttyUSB0?uart-baudrate=460800' --backbone-interface eth0` |
| Form network | `ot-ctl dataset init new; dataset channel 25; dataset panid 0xEFEB; dataset commit active; ifconfig up; thread start` |
| Enable services | `ot-ctl srp server enable` |
| Extract creds | `python tools/lab_thread_creds.py --exec "wsl -d Ubuntu-24.04 docker exec otbr ot-ctl" --write-overlay` |
| Is it up? | `./tools/lab_otbr_up.ps1 -Action health` |
| Reach node (in WSL) | `python3 tools/diag_get.py --local --addr <node-OMR>` |
| Teardown | `docker stop otbr && docker rm otbr` ; `usbipd detach --busid <BUSID>` |

## Sources

- [darkxst/silabs-firmware-builder — ot-rcp for zbdonglee](https://github.com/darkxst/silabs-firmware-builder/tree/main/firmware_builds/zbdonglee) (fork of [NabuCasa/silabs-firmware-builder](https://github.com/NabuCasa/silabs-firmware-builder))
- [NabuCasa/universal-silabs-flasher](https://github.com/NabuCasa/universal-silabs-flasher)
- [usbipd-win — WSL support](https://github.com/dorssel/usbipd-win/wiki/WSL-support) · [Microsoft: Connect USB devices to WSL](https://learn.microsoft.com/en-us/windows/wsl/connect-usb)
- [OpenThread Border Router — Docker guide](https://openthread.io/guides/border-router/docker)
- [Silabs AN1256 — Using SL RCP with OTBR](https://www.silabs.com/documents/public/application-notes/an1256-using-sl-rcp-with-openthread-border-router.pdf)
- [WSL mirrored networking IPv6 issues #11679](https://github.com/microsoft/WSL/issues/11679) · [#10663](https://github.com/microsoft/WSL/issues/10663)
