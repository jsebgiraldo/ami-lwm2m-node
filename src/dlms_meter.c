/*
 * DLMS Meter Reader — Microstar Smart Meter via RS485
 *
 * Full DLMS/COSEM client over HDLC serial. Reads 3-phase electrical
 * measurements and maps them to LwM2M Object 10242 resources.
 *
 * OBIS → LwM2M Resource Mapping:
 * ┌─────────────┬──────────────────────────┬────────────────────┬─────┐
 * │ OBIS Code   │ Description              │ LwM2M Resource     │ RID │
 * ├─────────────┼──────────────────────────┼────────────────────┼─────┤
 * │ 1-1:32.7.0  │ Voltage Phase A          │ PM_TENSION_R       │  4  │
 * │ 1-1:52.7.0  │ Voltage Phase B          │ PM_TENSION_S       │ 14  │
 * │ 1-1:72.7.0  │ Voltage Phase C          │ PM_TENSION_T       │ 24  │
 * │ 1-1:31.7.0  │ Current Phase A          │ PM_CURRENT_R       │  5  │
 * │ 1-1:51.7.0  │ Current Phase B          │ PM_CURRENT_S       │ 15  │
 * │ 1-1:71.7.0  │ Current Phase C          │ PM_CURRENT_T       │ 25  │
 * │ 1-1:21.7.0  │ Active Power Phase A     │ PM_ACTIVE_POWER_R  │  6  │
 * │ 1-1:41.7.0  │ Active Power Phase B     │ PM_ACTIVE_POWER_S  │ 16  │
 * │ 1-1:61.7.0  │ Active Power Phase C     │ PM_ACTIVE_POWER_T  │ 26  │
 * │ 1-1:23.7.0  │ Reactive Power Phase A   │ PM_REACTIVE_POWER_R│  7  │
 * │ 1-1:43.7.0  │ Reactive Power Phase B   │ PM_REACTIVE_POWER_S│ 17  │
 * │ 1-1:63.7.0  │ Reactive Power Phase C   │ PM_REACTIVE_POWER_T│ 27  │
 * │ 1-1:29.7.0  │ Apparent Power Phase A   │ PM_APPARENT_POWER_R│ 10  │
 * │ 1-1:49.7.0  │ Apparent Power Phase B   │ PM_APPARENT_POWER_S│ 20  │
 * │ 1-1:69.7.0  │ Apparent Power Phase C   │ PM_APPARENT_POWER_T│ 30  │
 * │ 1-1:33.7.0  │ Power Factor Phase A     │ PM_POWER_FACTOR_R  │ 11  │
 * │ 1-1:53.7.0  │ Power Factor Phase B     │ PM_POWER_FACTOR_S  │ 21  │
 * │ 1-1:73.7.0  │ Power Factor Phase C     │ PM_POWER_FACTOR_T  │ 31  │
 * │ 1-1:1.7.0   │ Total Active Power       │ PM_3P_ACTIVE_POWER │ 34  │
 * │ 1-1:3.7.0   │ Total Reactive Power     │ PM_3P_REACTIVE_PW  │ 35  │
 * │ 1-1:9.7.0   │ Total Apparent Power     │ PM_3P_APPARENT_PW  │ 38  │
 * │ 1-1:13.7.0  │ Total Power Factor       │ PM_3P_POWER_FACTOR │ 39  │
 * │ 1-1:1.8.0   │ Active Energy Import     │ PM_ACTIVE_ENERGY   │ 41  │
 * │ 1-1:3.8.0   │ Reactive Energy          │ PM_REACTIVE_ENERGY │ 42  │
 * │ 1-1:9.8.0   │ Apparent Energy          │ PM_APPARENT_ENERGY │ 45  │
 * │ 1-1:14.7.0  │ Frequency                │ PM_FREQUENCY       │ 49  │
 * │ 1-1:91.7.0  │ Neutral Current          │ PM_NEUTRAL_CURRENT │ 50  │
 * └─────────────┴──────────────────────────┴────────────────────┴─────┘
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>
#include <string.h>
#include <math.h>

#include "dlms_meter.h"
#include "dlms_hdlc.h"
#include "dlms_cosem.h"
#include "rs485_uart.h"
#include "lwm2m_obj_power_meter.h"
#include "lwm2m_observation.h"

LOG_MODULE_REGISTER(dlms_meter, LOG_LEVEL_INF);

/* ---- OBIS → Reading mapping entry ---- */
struct obis_mapping {
	struct obis_code obis;          /* OBIS code to read */
	uint16_t         class_id;      /* DLMS interface class (3=Register, 4=ExtRegister) */
	const char      *name;          /* Human-readable name */
	size_t           offset;        /* Offset into meter_readings struct */
};

/* Helper macro: offset of a double field in meter_readings */
#define MR_OFF(field) offsetof(struct meter_readings, field)

/*
 * OBIS codes to read from the Microstar meter.
 * Class 3 = Register (attribute 2 = value, attribute 3 = scaler_unit)
 * Class 4 = Extended Register (same attributes)
 *
 * We read instantaneous values (.7.0) and cumulative energy (.8.0).
 */
static const struct obis_mapping obis_table[] = {
	/* Phase A (R) */
	{ .obis = {1,1,32,7,0,255}, .class_id = 3, .name = "Voltage_R",
	  .offset = MR_OFF(voltage_r) },
	{ .obis = {1,1,31,7,0,255}, .class_id = 3, .name = "Current_R",
	  .offset = MR_OFF(current_r) },
	{ .obis = {1,1,21,7,0,255}, .class_id = 3, .name = "ActivePower_R",
	  .offset = MR_OFF(active_power_r) },
	{ .obis = {1,1,23,7,0,255}, .class_id = 3, .name = "ReactivePower_R",
	  .offset = MR_OFF(reactive_power_r) },
	{ .obis = {1,1,29,7,0,255}, .class_id = 3, .name = "ApparentPower_R",
	  .offset = MR_OFF(apparent_power_r) },
	{ .obis = {1,1,33,7,0,255}, .class_id = 3, .name = "PowerFactor_R",
	  .offset = MR_OFF(power_factor_r) },

	/* Phase B (S) */
	{ .obis = {1,1,52,7,0,255}, .class_id = 3, .name = "Voltage_S",
	  .offset = MR_OFF(voltage_s) },
	{ .obis = {1,1,51,7,0,255}, .class_id = 3, .name = "Current_S",
	  .offset = MR_OFF(current_s) },
	{ .obis = {1,1,41,7,0,255}, .class_id = 3, .name = "ActivePower_S",
	  .offset = MR_OFF(active_power_s) },
	{ .obis = {1,1,43,7,0,255}, .class_id = 3, .name = "ReactivePower_S",
	  .offset = MR_OFF(reactive_power_s) },
	{ .obis = {1,1,49,7,0,255}, .class_id = 3, .name = "ApparentPower_S",
	  .offset = MR_OFF(apparent_power_s) },
	{ .obis = {1,1,53,7,0,255}, .class_id = 3, .name = "PowerFactor_S",
	  .offset = MR_OFF(power_factor_s) },

	/* Phase C (T) */
	{ .obis = {1,1,72,7,0,255}, .class_id = 3, .name = "Voltage_T",
	  .offset = MR_OFF(voltage_t) },
	{ .obis = {1,1,71,7,0,255}, .class_id = 3, .name = "Current_T",
	  .offset = MR_OFF(current_t) },
	{ .obis = {1,1,61,7,0,255}, .class_id = 3, .name = "ActivePower_T",
	  .offset = MR_OFF(active_power_t) },
	{ .obis = {1,1,63,7,0,255}, .class_id = 3, .name = "ReactivePower_T",
	  .offset = MR_OFF(reactive_power_t) },
	{ .obis = {1,1,69,7,0,255}, .class_id = 3, .name = "ApparentPower_T",
	  .offset = MR_OFF(apparent_power_t) },
	{ .obis = {1,1,73,7,0,255}, .class_id = 3, .name = "PowerFactor_T",
	  .offset = MR_OFF(power_factor_t) },

	/* Totals */
	{ .obis = {1,1,1,7,0,255}, .class_id = 3, .name = "TotalActivePower",
	  .offset = MR_OFF(total_active_power) },
	{ .obis = {1,1,3,7,0,255}, .class_id = 3, .name = "TotalReactivePower",
	  .offset = MR_OFF(total_reactive_power) },
	{ .obis = {1,1,9,7,0,255}, .class_id = 3, .name = "TotalApparentPower",
	  .offset = MR_OFF(total_apparent_power) },
	{ .obis = {1,1,13,7,0,255}, .class_id = 3, .name = "TotalPowerFactor",
	  .offset = MR_OFF(total_power_factor) },

	/* Energy */
	{ .obis = {1,1,1,8,0,255}, .class_id = 3, .name = "ActiveEnergy",
	  .offset = MR_OFF(active_energy) },
	{ .obis = {1,1,3,8,0,255}, .class_id = 3, .name = "ReactiveEnergy",
	  .offset = MR_OFF(reactive_energy) },
	{ .obis = {1,1,9,8,0,255}, .class_id = 3, .name = "ApparentEnergy",
	  .offset = MR_OFF(apparent_energy) },

	/* Other */
	{ .obis = {1,1,14,7,0,255}, .class_id = 3, .name = "Frequency",
	  .offset = MR_OFF(frequency) },
	{ .obis = {1,1,91,7,0,255}, .class_id = 3, .name = "NeutralCurrent",
	  .offset = MR_OFF(neutral_current) },
};

#define OBIS_TABLE_SIZE  ARRAY_SIZE(obis_table)

/* ---- Module state ---- */
static enum meter_state state = METER_DISCONNECTED;
static struct meter_config cfg;
static uint8_t hdlc_send_seq;
static uint8_t hdlc_recv_seq;
static uint8_t cosem_invoke_id;

/* Frame buffers */
static uint8_t tx_buf[HDLC_MAX_FRAME_LEN];
static uint8_t rx_buf[HDLC_MAX_FRAME_LEN];

/* Computed HDLC addresses */
static uint8_t hdlc_client_addr;
static uint8_t hdlc_server_addr;

/* Scaler cache: 10^scaler for each OBIS entry (read once, reuse) */
static double scaler_cache[ARRAY_SIZE(obis_table)];
static bool   scaler_cached[ARRAY_SIZE(obis_table)];

/*
 * Runtime skip bitmap: OBIS codes that return "data access error" (e.g.,
 * Phase S/T voltage/current on a single-phase meter) are auto-skipped in
 * subsequent poll cycles to avoid wasting ~430 ms per unsupported register.
 */
static bool obis_skip[ARRAY_SIZE(obis_table)];

/*
 * LLC header for DLMS/COSEM over HDLC (IEC 62056-46 §6.4.4.4.3.2).
 * I-frames carrying COSEM APDUs MUST be preceded by the LLC sublayer header.
 *   Client → Server : E6 E6 00
 *   Server → Client : E6 E7 00
 */
#define LLC_HDR_LEN  3
static const uint8_t llc_send_hdr[] = { 0xE6, 0xE6, 0x00 };

/**
 * Build an HDLC I-frame with the mandatory LLC header prepended to the
 * COSEM PDU.  Increments hdlc_send_seq automatically on success.
 */
static int build_cosem_iframe(const uint8_t *pdu, int pdu_len)
{
	uint8_t llc_pdu[HDLC_MAX_INFO_LEN];
	size_t  total = LLC_HDR_LEN + pdu_len;

	if (total > sizeof(llc_pdu)) {
		return -ENOMEM;
	}

	memcpy(llc_pdu, llc_send_hdr, LLC_HDR_LEN);
	memcpy(llc_pdu + LLC_HDR_LEN, pdu, pdu_len);

	int ret = hdlc_build_iframe(tx_buf, sizeof(tx_buf),
				    hdlc_client_addr, hdlc_server_addr,
				    hdlc_send_seq, hdlc_recv_seq,
				    llc_pdu, total);
	if (ret > 0) {
		hdlc_send_seq = (hdlc_send_seq + 1) & 0x07;
	}
	return ret;
}

/**
 * After receiving an I-frame response, update the HDLC receive sequence
 * number and strip the 3-byte LLC header so callers see pure COSEM PDU.
 */
static void strip_iframe_llc(struct hdlc_frame *resp)
{
	/* Update receive sequence from server's send sequence */
	if ((resp->control & 0x01) == 0) {
		hdlc_recv_seq = ((resp->control >> 1) & 0x07) + 1;
		hdlc_recv_seq &= 0x07;
	}

	/* Strip LLC header (E6 E6/E7 00) if present */
	if (resp->info_len >= LLC_HDR_LEN &&
	    resp->info[0] == 0xE6 &&
	    (resp->info[1] == 0xE6 || resp->info[1] == 0xE7)) {
		resp->info_len -= LLC_HDR_LEN;
		memmove(resp->info, resp->info + LLC_HDR_LEN, resp->info_len);
	}
}

/* ---- Default config ---- */
static void set_defaults(void)
{
	cfg.client_sap = 1;
	cfg.server_logical = 0;   /* CRITICAL: Microstar requires logical=0 */
	cfg.server_physical = 1;
	strncpy(cfg.password, "22222222", sizeof(cfg.password));
	cfg.max_info_len = 128;
	cfg.response_timeout_ms = 5000;
	cfg.inter_frame_delay_ms = 30;  /* 30 ms — meter responds in ~250 ms */
}

/* ---- Send frame and receive response ---- */
static int transact(const uint8_t *tx, int tx_len, struct hdlc_frame *resp)
{
	int ret;

	/* Flush RX before sending */
	rs485_flush_rx();

	LOG_DBG("TX %d bytes to meter", tx_len);
	LOG_HEXDUMP_DBG(tx, tx_len, "HDLC TX");

	/* Send frame */
	ret = rs485_send(tx, tx_len);
	if (ret < 0) {
		LOG_ERR("RS485 send failed: %d", ret);
		return ret;
	}

	/* Wait for response */
	k_sleep(K_MSEC(cfg.inter_frame_delay_ms));

	ret = rs485_recv(rx_buf, sizeof(rx_buf), cfg.response_timeout_ms);
	if (ret <= 0) {
		LOG_ERR("RS485 recv failed: %d (timeout=%dms)", ret, cfg.response_timeout_ms);
		return ret < 0 ? ret : -ENODATA;
	}
	LOG_DBG("RX %d bytes from meter", ret);
	LOG_HEXDUMP_DBG(rx_buf, ret, "HDLC RX");

	if (ret < 9) {
		LOG_WRN("Response too short: %d bytes", ret);
		return -EPROTO;
	}

	/* Find HDLC frame in received data */
	size_t fstart, flen;
	int rc = hdlc_find_frame(rx_buf, ret, &fstart, &flen);
	if (rc < 0) {
		LOG_ERR("No HDLC frame found in response");
		return rc;
	}

	/* Parse the frame */
	rc = hdlc_parse_frame(&rx_buf[fstart], flen, resp);
	if (rc < 0) {
		LOG_ERR("HDLC parse failed: %d", rc);
		return rc;
	}

	return 0;
}

/* ---- Public API ---- */

int meter_init(void)
{
	int ret;

	set_defaults();

	ret = rs485_init();
	if (ret < 0) {
		LOG_ERR("RS485 init failed: %d", ret);
		return ret;
	}

	memset(scaler_cached, 0, sizeof(scaler_cached));
	memset(obis_skip, 0, sizeof(obis_skip));
	state = METER_DISCONNECTED;

	LOG_INF("DLMS Meter Reader initialized");
	LOG_INF("  Client SAP: %u, Server: logical=%u physical=%u",
		cfg.client_sap, cfg.server_logical, cfg.server_physical);
	LOG_INF("  Password: %s", cfg.password);
	LOG_INF("  OBIS codes to read: %u", (unsigned)OBIS_TABLE_SIZE);

	return 0;
}

void meter_set_config(const struct meter_config *new_cfg)
{
	if (new_cfg) {
		memcpy(&cfg, new_cfg, sizeof(cfg));
	} else {
		set_defaults();
	}
}

int meter_connect(void)
{
	struct hdlc_frame resp;
	int ret;

	if (state >= METER_HDLC_CONNECTED) {
		LOG_WRN("Already connected, disconnecting first");
		meter_disconnect();
	}

	/* Compute HDLC addresses.
	 * The server address combines logical + physical per IEC 62056-46:
	 *   combined = (logical << 7) | physical
	 * Then HDLC-encode: if combined < 128, 1-byte: ((combined << 1) | 1)
	 * This matches the working Python dlms_reader.py reference.
	 */
	hdlc_client_addr = HDLC_CLIENT_ADDR(cfg.client_sap);
	uint16_t combined_server = ((uint16_t)cfg.server_logical << 7) | cfg.server_physical;
	if (combined_server < 0x80) {
		hdlc_server_addr = (uint8_t)((combined_server << 1) | 1);
	} else {
		/* 2-byte address needed — not yet supported, fall back */
		LOG_WRN("Server address needs 2-byte encoding (combined=0x%04X), using 1-byte",
			combined_server);
		hdlc_server_addr = HDLC_SERVER_ADDR_1B(cfg.server_logical);
	}
	hdlc_send_seq = 0;
	hdlc_recv_seq = 0;
	cosem_invoke_id = 0;

	LOG_INF("Connecting to meter... (client=0x%02X server=0x%02X, logical=%u physical=%u)",
		hdlc_client_addr, hdlc_server_addr, cfg.server_logical, cfg.server_physical);

	/* ---- Step 1: HDLC SNRM ---- */
	/* Send MINIMAL SNRM (no info field) — Microstar responds reliably
	 * to a minimal SNRM. The working Python reference also sends
	 * SNRM without negotiation parameters by default.
	 */
	ret = hdlc_build_snrm(tx_buf, sizeof(tx_buf),
			      hdlc_client_addr, hdlc_server_addr, NULL);
	if (ret < 0) {
		LOG_ERR("Failed to build SNRM: %d", ret);
		return ret;
	}

	ret = transact(tx_buf, ret, &resp);
	if (ret < 0) {
		LOG_ERR("SNRM transaction failed: %d", ret);
		state = METER_ERROR;
		return ret;
	}

	if (resp.control != HDLC_CTRL_UA) {
		LOG_ERR("Expected UA (0x73), got 0x%02X", resp.control);
		state = METER_ERROR;
		return -EPROTO;
	}

	state = METER_HDLC_CONNECTED;
	LOG_INF("HDLC connected (UA received)");

	/* ---- Step 2: COSEM AARQ ---- */
	/* Small delay for meter to finish SNRM processing */
	k_sleep(K_MSEC(100));  /* Brief settle time after SNRM/UA */

	uint8_t aarq_pdu[128];
	ret = cosem_build_aarq(aarq_pdu, sizeof(aarq_pdu),
			       (const uint8_t *)cfg.password,
			       strlen(cfg.password));
	if (ret < 0) {
		LOG_ERR("Failed to build AARQ: %d", ret);
		return ret;
	}

	ret = build_cosem_iframe(aarq_pdu, ret);
	if (ret < 0) {
		LOG_ERR("Failed to build I-frame for AARQ: %d", ret);
		return ret;
	}

	ret = transact(tx_buf, ret, &resp);
	if (ret < 0) {
		LOG_ERR("AARQ transaction failed: %d", ret);
		state = METER_ERROR;
		return ret;
	}

	/* Strip LLC header and update HDLC sequence */
	strip_iframe_llc(&resp);

	/* Parse AARE from info field */
	ret = cosem_parse_aare(resp.info, resp.info_len);
	if (ret < 0) {
		LOG_ERR("AARE rejected: %d", ret);
		state = METER_ERROR;
		return ret;
	}

	state = METER_ASSOCIATED;
	LOG_INF("COSEM association established (AARE accepted)");

	return 0;
}

int meter_disconnect(void)
{
	struct hdlc_frame resp;
	int ret;

	if (state == METER_DISCONNECTED) {
		return 0;
	}

	if (state >= METER_ASSOCIATED) {
		/* Send RLRQ (Release Request) with LLC header */
		uint8_t rlrq_pdu[8];
		int rlrq_len = cosem_build_rlrq(rlrq_pdu, sizeof(rlrq_pdu));
		if (rlrq_len > 0) {
			ret = build_cosem_iframe(rlrq_pdu, rlrq_len);
			if (ret > 0) {
				transact(tx_buf, ret, &resp);
				/* Ignore errors on disconnect */
			}
		}
	}

	/* Send HDLC DISC */
	ret = hdlc_build_disc(tx_buf, sizeof(tx_buf),
			      hdlc_client_addr, hdlc_server_addr);
	if (ret > 0) {
		transact(tx_buf, ret, &resp);
		/* Ignore errors */
	}

	state = METER_DISCONNECTED;
	hdlc_send_seq = 0;
	hdlc_recv_seq = 0;

	LOG_INF("Meter disconnected");
	return 0;
}

/* ---- Read a single OBIS value ---- */
static int read_obis_value(const struct obis_mapping *entry,
			   struct cosem_get_result *result)
{
	struct hdlc_frame resp;
	uint8_t get_pdu[32];
	int ret;

	struct cosem_attr_desc attr = {
		.class_id = entry->class_id,
		.obis = entry->obis,
		.attribute_id = 2,  /* Value attribute */
	};

	/* Build GET.request */
	ret = cosem_build_get_request(get_pdu, sizeof(get_pdu),
				      cosem_invoke_id++, &attr);
	if (ret < 0) {
		return ret;
	}

	/* Wrap in HDLC I-frame with LLC header */
	ret = build_cosem_iframe(get_pdu, ret);
	if (ret < 0) {
		return ret;
	}

	/* Transact */
	ret = transact(tx_buf, ret, &resp);
	if (ret < 0) {
		return ret;
	}

	/* Strip LLC header and update sequence */
	strip_iframe_llc(&resp);

	/* Parse GET.response */
	ret = cosem_parse_get_response(resp.info, resp.info_len, result);
	return ret;
}

/* ---- Convert COSEM value to double, applying scaler ---- */
static double value_to_double(const struct cosem_get_result *result,
			      int table_idx)
{
	double raw_val = 0.0;

	switch (result->data_type) {
	case COSEM_TYPE_UINT8:
	case COSEM_TYPE_UINT16:
	case COSEM_TYPE_UINT32:
	case COSEM_TYPE_UINT64:
	case COSEM_TYPE_ENUM:
		raw_val = (double)result->value.u64;
		break;

	case COSEM_TYPE_INT8:
	case COSEM_TYPE_INT16:
	case COSEM_TYPE_INT32:
	case COSEM_TYPE_INT64:
		raw_val = (double)result->value.i64;
		break;

	case COSEM_TYPE_FLOAT32:
	case COSEM_TYPE_FLOAT64:
		raw_val = result->value.f64;
		break;

	default:
		LOG_WRN("Unexpected data type 0x%02X for %s",
			result->data_type, obis_table[table_idx].name);
		return 0.0;
	}

	/* Apply scaler if cached */
	if (table_idx >= 0 && (size_t)table_idx < OBIS_TABLE_SIZE &&
	    scaler_cached[table_idx]) {
		raw_val *= scaler_cache[table_idx];
	}

	return raw_val;
}

/* ---- Read scaler_unit (attribute 3) for a Register object ---- */
static int read_scaler_unit(int table_idx)
{
	struct hdlc_frame resp;
	uint8_t get_pdu[32];
	int ret;

	const struct obis_mapping *entry = &obis_table[table_idx];
	struct cosem_attr_desc attr = {
		.class_id = entry->class_id,
		.obis = entry->obis,
		.attribute_id = 3,  /* scaler_unit */
	};

	ret = cosem_build_get_request(get_pdu, sizeof(get_pdu),
				      cosem_invoke_id++, &attr);
	if (ret < 0) return ret;

	ret = build_cosem_iframe(get_pdu, ret);
	if (ret < 0) return ret;

	ret = transact(tx_buf, ret, &resp);
	if (ret < 0) return ret;

	/* Strip LLC header and update sequence */
	strip_iframe_llc(&resp);

	/*
	 * scaler_unit response is a structure {int8 scaler, enum unit}:
	 *   00  — Data choice
	 *   02  — structure tag
	 *   02  — 2 elements
	 *   0F XX  — int8 scaler
	 *   16 XX  — enum unit
	 */
	if (resp.info_len > 6 && resp.info[0] == COSEM_TAG_GET_RESPONSE) {
		/* Skip GET.response header: C4 01 <invoke_id> 00 */
		uint8_t *d = &resp.info[4];
		size_t dlen = resp.info_len - 4;

		if (dlen >= 6 && d[0] == COSEM_TYPE_STRUCTURE && d[1] == 0x02) {
			/* Parse scaler (int8) */
			if (d[2] == COSEM_TYPE_INT8 && dlen >= 6) {
				int8_t scaler = (int8_t)d[3];
				scaler_cache[table_idx] = pow(10.0, (double)scaler);
				scaler_cached[table_idx] = true;

				/* Parse unit (enum) */
				uint8_t unit = (d[4] == COSEM_TYPE_ENUM) ? d[5] : 0;
				LOG_DBG("  %s: scaler=%d (x%.6f) unit=%u",
					entry->name, scaler,
					scaler_cache[table_idx], unit);
				return 0;
			}
		}
	}

	/* Fallback: no scaler (multiply by 1) */
	scaler_cache[table_idx] = 1.0;
	scaler_cached[table_idx] = true;
	return 0;
}

int meter_read_all(struct meter_readings *readings)
{
	if (!readings) {
		return -EINVAL;
	}

	if (state != METER_ASSOCIATED) {
		LOG_ERR("Not associated with meter");
		return -ENOTCONN;
	}

	memset(readings, 0, sizeof(*readings));
	readings->timestamp_ms = k_uptime_get();

	/*
	 * Phase 1: Read scaler_unit for entries that haven't been cached yet.
	 * This only needs to happen once per connection.
	 */
	for (size_t i = 0; i < OBIS_TABLE_SIZE; i++) {
		if (obis_skip[i] || scaler_cached[i]) {
			continue;
		}
		int ret = read_scaler_unit(i);
		if (ret < 0) {
			LOG_WRN("Failed to read scaler for %s: %d",
				obis_table[i].name, ret);
			/* Use no scaling */
			scaler_cache[i] = 1.0;
			scaler_cached[i] = true;
		}
		k_sleep(K_MSEC(20));
	}

	/*
	 * Phase 2: Read all values
	 */
	int skip_count = 0;
	for (size_t i = 0; i < OBIS_TABLE_SIZE; i++) {
		if (obis_skip[i]) {
			skip_count++;
		}
	}
	int read_target = (int)OBIS_TABLE_SIZE - skip_count;
	LOG_INF("Reading %d OBIS codes from meter (skipping %d unsupported)...",
		read_target, skip_count);

	int64_t t_start = k_uptime_get();

	for (size_t i = 0; i < OBIS_TABLE_SIZE; i++) {
		if (obis_skip[i]) {
			continue;
		}

		struct cosem_get_result result;
		int ret = read_obis_value(&obis_table[i], &result);

		if (ret == 0 && result.success) {
			double val = value_to_double(&result, i);

			/* Write value to the correct field in readings */
			double *target = (double *)((uint8_t *)readings +
						    obis_table[i].offset);
			*target = val;
			readings->read_count++;

			LOG_DBG("  %s = %.3f", obis_table[i].name, val);
		} else {
			LOG_WRN("  %s: read failed (%d)", obis_table[i].name, ret);
			readings->error_count++;

			/* Auto-skip OBIS codes that the meter refuses (error 4) */
			if (ret == -EACCES) {
				obis_skip[i] = true;
				LOG_WRN("  %s: marked as unsupported — will skip",
					obis_table[i].name);
			}
		}

		k_sleep(K_MSEC(20));
	}

	int64_t elapsed = k_uptime_get() - t_start;
	LOG_INF("Value reads completed in %lld ms", elapsed);

	readings->valid = (readings->read_count > 0);

	LOG_INF("Meter read complete: %d/%d successful (%d skipped)",
		readings->read_count, read_target, skip_count);

	return readings->valid ? 0 : -EIO;
}

int meter_poll(struct meter_readings *readings)
{
	int ret;

	if (!readings) {
		return -EINVAL;
	}

	int64_t poll_start = k_uptime_get();
	LOG_INF("=== Meter poll cycle ===");

	/* Connect */
	ret = meter_connect();
	if (ret < 0) {
		LOG_ERR("Meter connect failed: %d", ret);
		meter_disconnect();
		return ret;
	}

	/* Read all values */
	ret = meter_read_all(readings);
	if (ret < 0) {
		LOG_ERR("Meter read failed: %d", ret);
	}

	/* Disconnect (always, even on error) */
	meter_disconnect();

	int64_t poll_ms = k_uptime_get() - poll_start;
	LOG_INF("=== Meter poll complete: %lld ms ===", poll_ms);

	return ret;
}

void meter_push_to_lwm2m(const struct meter_readings *readings)
{
	if (!readings || !readings->valid) {
		return;
	}

	/* ---- Phase R ---- */
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_TENSION_R_RID),
		       readings->voltage_r);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_CURRENT_R_RID),
		       readings->current_r);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_POWER_R_RID),
		       readings->active_power_r);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_REACTIVE_POWER_R_RID),
		       readings->reactive_power_r);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_APPARENT_POWER_R_RID),
		       readings->apparent_power_r);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_POWER_FACTOR_R_RID),
		       readings->power_factor_r);

	/* ---- Phase S ---- */
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_TENSION_S_RID),
		       readings->voltage_s);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_CURRENT_S_RID),
		       readings->current_s);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_POWER_S_RID),
		       readings->active_power_s);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_REACTIVE_POWER_S_RID),
		       readings->reactive_power_s);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_APPARENT_POWER_S_RID),
		       readings->apparent_power_s);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_POWER_FACTOR_S_RID),
		       readings->power_factor_s);

	/* ---- Phase T ---- */
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_TENSION_T_RID),
		       readings->voltage_t);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_CURRENT_T_RID),
		       readings->current_t);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_POWER_T_RID),
		       readings->active_power_t);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_REACTIVE_POWER_T_RID),
		       readings->reactive_power_t);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_APPARENT_POWER_T_RID),
		       readings->apparent_power_t);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_POWER_FACTOR_T_RID),
		       readings->power_factor_t);

	/* ---- Totals ---- */
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_3P_ACTIVE_POWER_RID),
		       readings->total_active_power);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_3P_REACTIVE_POWER_RID),
		       readings->total_reactive_power);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_3P_APPARENT_POWER_RID),
		       readings->total_apparent_power);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_3P_POWER_FACTOR_RID),
		       readings->total_power_factor);

	/* ---- Energy ---- */
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_ENERGY_RID),
		       readings->active_energy);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_REACTIVE_ENERGY_RID),
		       readings->reactive_energy);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_APPARENT_ENERGY_RID),
		       readings->apparent_energy);

	/* ---- Other ---- */
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_FREQUENCY_RID),
		       readings->frequency);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_NEUTRAL_CURRENT_RID),
		       readings->neutral_current);

	/* ---- Notify observers on key resources ---- */
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_TENSION_R_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_CURRENT_R_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_TENSION_S_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_CURRENT_S_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_TENSION_T_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_CURRENT_T_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_POWER_R_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_POWER_S_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_POWER_T_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_3P_ACTIVE_POWER_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_ENERGY_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_FREQUENCY_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_3P_POWER_FACTOR_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_NEUTRAL_CURRENT_RID);

	LOG_INF("LwM2M updated: V=%.1f/%.1f/%.1f  I=%.2f/%.2f/%.2f  "
		"P=%.2fkW  E=%.1fkWh  f=%.1fHz",
		readings->voltage_r, readings->voltage_s, readings->voltage_t,
		readings->current_r, readings->current_s, readings->current_t,
		readings->total_active_power, readings->active_energy,
		readings->frequency);
}

enum meter_state meter_get_state(void)
{
	return state;
}
