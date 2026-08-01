/*
 * Object 33001 gives the server LwM2M-mediated role control; this gives the
 * server a *direct* IPv6 read path. A minimal CoAP GET server on port 5685
 * exposing two resources, both independent of the LwM2M registration:
 *
 *   coap://[<node-ipv6>]:5685/diag  -> node status snapshot (JSON)
 *      {"fw":"0.7.15-diag","role":"Router","uptime_s":1234,"resets":7,
 *       "reg_ok":3,"recover":5,"wdog":0,"boot_burst":0,
 *       "partition":123456,"rloc16":"0x4c00"}
 *
 *   coap://[<node-ipv6>]:5685/ami   -> live AMI metering / active OBIS (JSON)
 *      {"valid":true,"vr":122.24,...,"ptot":0.03,"eact":61100.0,"freq":59.94,
 *       "reads":15,"errs":0,"mask":"0x0007ffff","age_s":3}
 *
 * Useful precisely for the "inactive on TB" nodes: if a node is still on the
 * Thread mesh it answers these GETs even when its LwM2M session is down, so the
 * server can tell "reachable but not registered" from "off the mesh" AND pull
 * the instantaneous meter reading without waiting on the LwM2M Observe cadence.
 */
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/coap.h>
#include <zephyr/net/coap_service.h>
#include <zephyr/net/openthread.h>
#include <openthread/instance.h>
#include <openthread/thread.h>
#include <stdio.h>
#include <string.h>

#include "coap_diag.h"
#include "dlms_meter.h"

LOG_MODULE_REGISTER(coap_diag, LOG_LEVEL_INF);

/* Diagnostic counter getters (defined in main.c). */
extern uint32_t lwm2m_diag_get_total_resets(void);
extern uint32_t lwm2m_diag_get_reg_success(void);
extern uint32_t lwm2m_diag_get_recover_count(void);
extern uint32_t lwm2m_diag_get_watchdog_count(void);
extern uint32_t lwm2m_diag_get_boot_burst(void);

/* Current meter readings snapshot (main.c). Reads last_readings, which is
 * populated in BOTH demo and real modes — see ami_get_last_readings(). */
extern bool ami_get_last_readings(struct meter_readings *out);

static const char *g_fw = "?";

/* CoAP diag server: distinct port so it never collides with the LwM2M client's
 * sockets. Auto-starts once the network interface is up. */
static uint16_t coap_diag_port = 5685;
COAP_SERVICE_DEFINE(diag_service, "::", &coap_diag_port, COAP_SERVICE_AUTOSTART);

/* Payload budget: leave headroom under the server message size for the CoAP
 * header + token + Content-Format option (~16 bytes) so append_payload never
 * overflows the response buffer. */
#define DIAG_JSON_MAX  (CONFIG_COAP_SERVER_MESSAGE_SIZE - 64)

static const char *role_str(otDeviceRole r)
{
	switch (r) {
	case OT_DEVICE_ROLE_DISABLED: return "Disabled";
	case OT_DEVICE_ROLE_DETACHED: return "Detached";
	case OT_DEVICE_ROLE_CHILD:    return "Child";
	case OT_DEVICE_ROLE_ROUTER:   return "Router";
	case OT_DEVICE_ROLE_LEADER:   return "Leader";
	default:                      return "?";
	}
}

/* Build + send a 2.05 Content response carrying `json` (len bytes) as
 * application/json, echoing the request token/mid and ACK-ing CON requests. */
static int diag_send_json(struct coap_resource *resource, struct coap_packet *request,
			  struct sockaddr *addr, socklen_t addr_len,
			  const char *json, int len)
{
	uint8_t data[CONFIG_COAP_SERVER_MESSAGE_SIZE];
	struct coap_packet response;
	uint8_t token[COAP_TOKEN_MAX_LEN];
	uint16_t id = coap_header_get_id(request);
	uint8_t tkl = coap_header_get_token(request, token);
	uint8_t type = (coap_header_get_type(request) == COAP_TYPE_CON)
		       ? COAP_TYPE_ACK : COAP_TYPE_NON_CON;

	int r = coap_packet_init(&response, data, sizeof(data), COAP_VERSION_1, type,
				 tkl, token, COAP_RESPONSE_CODE_CONTENT, id);
	if (r < 0) {
		return r;
	}
	r = coap_append_option_int(&response, COAP_OPTION_CONTENT_FORMAT,
				   COAP_CONTENT_FORMAT_APP_JSON);
	if (r < 0) {
		return r;
	}
	r = coap_packet_append_payload_marker(&response);
	if (r < 0) {
		return r;
	}
	r = coap_packet_append_payload(&response, json, len);
	if (r < 0) {
		return r;
	}
	return coap_resource_send(resource, &response, addr, addr_len, NULL);
}

static int diag_get(struct coap_resource *resource, struct coap_packet *request,
		    struct sockaddr *addr, socklen_t addr_len)
{
	otInstance *ot = openthread_get_default_instance();
	otDeviceRole role = ot ? otThreadGetDeviceRole(ot) : OT_DEVICE_ROLE_DISABLED;
	uint32_t partition = ot ? otThreadGetPartitionId(ot) : 0;
	uint16_t rloc16 = ot ? otThreadGetRloc16(ot) : 0;
	uint32_t up = (uint32_t)(k_uptime_get() / 1000);

	char json[DIAG_JSON_MAX];
	int len = snprintf(json, sizeof(json),
		"{\"fw\":\"%s\",\"role\":\"%s\",\"uptime_s\":%u,\"resets\":%u,"
		"\"reg_ok\":%u,\"recover\":%u,\"wdog\":%u,\"boot_burst\":%u,"
		"\"partition\":%u,\"rloc16\":\"0x%04x\"}",
		g_fw, role_str(role), up,
		lwm2m_diag_get_total_resets(), lwm2m_diag_get_reg_success(),
		lwm2m_diag_get_recover_count(), lwm2m_diag_get_watchdog_count(),
		lwm2m_diag_get_boot_burst(), partition, rloc16);
	if (len < 0 || len >= (int)sizeof(json)) {
		len = (int)sizeof(json) - 1;
	}
	return diag_send_json(resource, request, addr, addr_len, json, len);
}

static int ami_get(struct coap_resource *resource, struct coap_packet *request,
		   struct sockaddr *addr, socklen_t addr_len)
{
	struct meter_readings m;
	bool have = ami_get_last_readings(&m);

	char json[DIAG_JSON_MAX];
	int len;
	if (!have) {
		/* No successful DLMS read yet (no meter attached, or bus down). */
		len = snprintf(json, sizeof(json), "{\"valid\":false}");
	} else {
		int64_t age_s = (k_uptime_get() - m.timestamp_ms) / 1000;
		len = snprintf(json, sizeof(json),
			"{\"valid\":true,"
			"\"vr\":%.2f,\"vs\":%.2f,\"vt\":%.2f,"
			"\"ir\":%.3f,\"is\":%.3f,\"it\":%.3f,"
			"\"pr\":%.3f,\"ps\":%.3f,\"pt\":%.3f,"
			"\"ptot\":%.3f,\"qtot\":%.3f,\"stot\":%.3f,\"pf\":%.3f,"
			"\"eact\":%.3f,\"ereact\":%.3f,\"eapp\":%.3f,"
			"\"freq\":%.2f,"
			"\"reads\":%d,\"errs\":%d,\"mask\":\"0x%08x\",\"age_s\":%lld}",
			m.voltage_r, m.voltage_s, m.voltage_t,
			m.current_r, m.current_s, m.current_t,
			m.active_power_r, m.active_power_s, m.active_power_t,
			m.total_active_power, m.total_reactive_power,
			m.total_apparent_power, m.total_power_factor,
			m.active_energy, m.reactive_energy, m.apparent_energy,
			m.frequency,
			m.read_count, m.error_count, m.field_mask,
			(long long)age_s);
	}
	if (len < 0 || len >= (int)sizeof(json)) {
		len = (int)sizeof(json) - 1;
	}
	return diag_send_json(resource, request, addr, addr_len, json, len);
}

static const char * const diag_path[] = { "diag", NULL };
COAP_RESOURCE_DEFINE(diag_resource, diag_service, {
	.get = diag_get,
	.path = diag_path,
});

static const char * const ami_path[] = { "ami", NULL };
COAP_RESOURCE_DEFINE(ami_resource, diag_service, {
	.get = ami_get,
	.path = ami_path,
});

int coap_diag_init(const char *fw_version)
{
	if (fw_version) {
		g_fw = fw_version;
	}
	LOG_INF("CoAP diag GET server up on [::]:%u  /diag (node) + /ami (meter)",
		coap_diag_port);
	return 0;
}
