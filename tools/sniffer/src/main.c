/*
 * AMI 802.15.4 Sniffer — passive promiscuous capture for Wireshark.
 *
 * Runs on an ESP32-C6 DevKit (the COM62 board). Puts the IEEE 802.15.4
 * radio in promiscuous mode on a fixed channel and emits every received
 * frame over the console UART as a single hex line:
 *
 *     $<hexframe>|<rssi_dbm>|<lqi>\n
 *
 * A one-shot banner is printed at boot so the host tool can confirm the
 * link and channel:
 *
 *     #AMI-SNIFFER channel=<n> promiscuous=1
 *
 * The companion host tool tools/sniffer_capture.py parses these lines and
 * builds a DLT_IEEE802_15_4_TAP pcap (carries per-frame RSSI / LQI /
 * channel metadata) for live or offline Wireshark analysis.
 *
 * Frame framing notes:
 *   - The ESP32 IEEE 802.15.4 HAL validates and strips the on-air FCS, and
 *     reports RSSI/LQI in frame metadata instead. So the bytes we emit are
 *     the MHR + MAC payload only, NO FCS. The host pcap is tagged
 *     FCS-type = 0 ("not present") accordingly.
 *   - RX frames are queued to a FIFO and serialised by a dedicated output
 *     thread — the radio RX path is never blocked on UART I/O.
 *
 * Build & flash (standalone Zephyr app — NOT part of the node firmware):
 *     west build -p always -b esp32c6_devkitc tools/sniffer
 *     west flash
 *
 * To sniff a different channel, change SNIFFER_CHANNEL below and rebuild.
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/net/ieee802154_radio.h>
#include <zephyr/net/ieee802154_pkt.h>
#include <zephyr/net/net_pkt.h>
#include <zephyr/sys/printk.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(sniffer, LOG_LEVEL_INF);

/* UNAL-R1000 production mesh runs on IEEE 802.15.4 channel 21. */
#define SNIFFER_CHANNEL 21

/* Worst-case 802.15.4 frame is 127 bytes: 2 hex chars each + "$" + the
 * "|<rssi>|<lqi>\n" suffix + NUL. */
#define HEXLINE_MAX (1 + 2 * 127 + 16)

#define OUT_THREAD_STACK 2048
#define OUT_THREAD_PRIO  K_PRIO_PREEMPT(7)

static const struct device *const radio_dev =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_ieee802154));
static struct ieee802154_radio_api *radio_api;

static const char hexd[] = "0123456789abcdef";

/* RX frames are queued here by net_recv_data() and drained by out_thread.
 * net_pkt is k_fifo-compatible (reserved first word), same pattern as the
 * upstream wpan_serial sample. */
static K_FIFO_DEFINE(capture_fifo);
static K_THREAD_STACK_DEFINE(out_stack, OUT_THREAD_STACK);
static struct k_thread out_thread_data;

/*
 * In RAW mode (CONFIG_IEEE802154_RAW_MODE) the radio driver delivers every
 * received frame here, bypassing all L2 processing. We override the weak
 * net_recv_data() symbol — the same hook the wpan_serial sample uses.
 *
 * Runs in the driver's RX context: do NOT block on UART here. Hand the pkt
 * to the output thread and return immediately. If the output thread falls
 * behind, the FIFO drains slowly and the driver runs out of RX buffers —
 * it then drops new frames gracefully instead of stalling the radio.
 */
int net_recv_data(struct net_if *iface, struct net_pkt *pkt)
{
	ARG_UNUSED(iface);
	k_fifo_put(&capture_fifo, pkt);   /* pkt is unref'd by out_thread */
	return 0;
}

/*
 * Required by the IEEE 802.15.4 stack in RAW mode. The sniffer never
 * transmits, so ACK handling is irrelevant — just let the stack continue.
 */
enum net_verdict ieee802154_handle_ack(struct net_if *iface, struct net_pkt *pkt)
{
	ARG_UNUSED(iface);
	ARG_UNUSED(pkt);
	return NET_CONTINUE;
}

static void out_thread(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	static char line[HEXLINE_MAX];

	for (;;) {
		struct net_pkt *pkt = k_fifo_get(&capture_fifo, K_FOREVER);
		size_t pos = 0;

		line[pos++] = '$';
		for (struct net_buf *b = pkt->buffer; b != NULL; b = b->frags) {
			for (uint16_t i = 0;
			     i < b->len && pos + 2 < sizeof(line); i++) {
				line[pos++] = hexd[b->data[i] >> 4];
				line[pos++] = hexd[b->data[i] & 0x0f];
			}
		}
		line[pos] = '\0';

		int16_t rssi = net_pkt_ieee802154_rssi_dbm(pkt);
		uint8_t lqi  = net_pkt_ieee802154_lqi(pkt);

		net_pkt_unref(pkt);

		/* One printk per frame — char-by-char would bottleneck the
		 * UART and let the FIFO back up under load. */
		printk("%s|%d|%u\n", line, (int)rssi, (unsigned int)lqi);
	}
}

int main(void)
{
	if (!device_is_ready(radio_dev)) {
		LOG_ERR("ieee802154 radio device not ready");
		return -1;
	}
	radio_api = (struct ieee802154_radio_api *)radio_dev->api;

	/* Promiscuous mode: capture every frame on-channel regardless of
	 * destination address, PAN ID, or frame type. Without it the HW
	 * frame filter drops everything not addressed to this device. */
	struct ieee802154_config cfg = { .promiscuous = true };
	int ret = radio_api->configure(radio_dev,
				       IEEE802154_CONFIG_PROMISCUOUS, &cfg);
	if (ret) {
		LOG_WRN("promiscuous configure failed: %d "
			"(capture will be filtered!)", ret);
	}

	ret = radio_api->set_channel(radio_dev, SNIFFER_CHANNEL);
	if (ret) {
		LOG_ERR("set_channel(%d) failed: %d", SNIFFER_CHANNEL, ret);
		return -1;
	}

	ret = radio_api->start(radio_dev);
	if (ret) {
		LOG_ERR("radio start failed: %d", ret);
		return -1;
	}

	k_thread_create(&out_thread_data, out_stack,
			K_THREAD_STACK_SIZEOF(out_stack),
			out_thread, NULL, NULL, NULL,
			OUT_THREAD_PRIO, 0, K_NO_WAIT);
	k_thread_name_set(&out_thread_data, "sniffer_out");

	/* Banner the host tool waits for to confirm link + channel. */
	printk("#AMI-SNIFFER channel=%d promiscuous=1\n", SNIFFER_CHANNEL);
	LOG_INF("AMI 802.15.4 sniffer up — channel %d, promiscuous mode",
		SNIFFER_CHANNEL);
	return 0;
}
