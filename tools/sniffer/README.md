# AMI 802.15.4 Sniffer

Passive promiscuous IEEE 802.15.4 capture for diagnosing the Thread mesh
air interface — why nodes drop off the OTBR, retransmission storms, CCA
failures, beacon/MLE traffic, interference.

Runs as a **standalone Zephyr app** on a dedicated ESP32-C6 DevKit (the
COM62 board). It is *not* part of the node firmware — it joins no network,
transmits nothing, and only listens.

## How it works

```
ESP32-C6 DevKit                 host PC
┌────────────────┐             ┌──────────────────────────┐
│ 15.4 radio     │  CH343 UART │ tools/sniffer_capture.py  │   ┌───────────┐
│  promiscuous   │═════════════│  parse → PCAP (DLT 283)   │──▶│ Wireshark │
│  channel 21    │  1 Mbaud    │                          │   └───────────┘
└────────────────┘             └──────────────────────────┘
```

The firmware puts the radio in **promiscuous mode** on a fixed channel,
and in Zephyr's `IEEE802154_RAW_MODE` every received frame is delivered
straight to `net_recv_data()`. Each frame is emitted on the console UART as:

```
$<hexframe>|<rssi_dbm>|<lqi>
```

The host tool wraps each frame in a `DLT_IEEE802_15_4_TAP` (link type 283)
PCAP record, which carries per-frame **RSSI, LQI and channel** as TLV
metadata — exactly what you want for "is the air healthy" analysis.

Frames are FCS-stripped: the ESP32 HAL validates the on-air FCS, drops
bad-CRC frames, and reports RSSI/LQI in metadata instead. The PCAP is
tagged FCS-type = 0 accordingly.

## Build & flash

```sh
# from the west workspace (ZEPHYR_BASE set)
west build -p always -b esp32c6_devkitc tools/sniffer
west flash
```

To sniff a different channel, edit `SNIFFER_CHANNEL` in `src/main.c` and
rebuild. The production UNAL-R1000 mesh is on **channel 21**.

## Capture

```sh
# Capture to a file, analyse later:
python tools/sniffer_capture.py --com COM62 --pcap mesh.pcap

# Live, straight into Wireshark:
python tools/sniffer_capture.py --com COM62 -w - | wireshark -k -i -

# Live + rolling stats on stderr (frames/s, per-type counts):
python tools/sniffer_capture.py --com COM62 --pcap mesh.pcap --stats
```

## What to look for in Wireshark

| Symptom in capture | Likely meaning |
|---|---|
| Many `Ack`-less `Data` frames + retransmits (same seq #) | Marginal link / interference — node will drop |
| `RSS` (TAP metadata) below ~−85 dBm for a node | Out of comfortable range — relocate or add a router |
| Bursts of `MAC-Cmd` Beacon Request / Parent Request | Node detached, re-attaching repeatedly |
| Frames from an unexpected PAN ID on channel 21 | Co-channel interference from a foreign 15.4 network |
| Long gaps then a burst from one short addr | Node sleeping/hanging then waking — correlate with USB-hang reports |

Filter examples: `wpan.dst_pan == 0x41ae` (UNAL-R1000 only),
`wpan.frame_type == 0x3` (MAC commands), `wpan.seq_no` (track retransmits).
