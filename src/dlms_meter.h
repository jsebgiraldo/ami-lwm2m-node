/*
 * DLMS Meter Reader — High-level interface for Microstar smart meter
 *
 * Orchestrates RS485 → HDLC → COSEM to read OBIS code values and
 * maps them to LwM2M Object 10242 (3-Phase Power Meter) resources.
 *
 * Flow:
 * 1. HDLC SNRM → UA (establish data link)
 * 2. COSEM AARQ → AARE (establish application association, LLS auth)
 * 3. For each OBIS code: GET.request → GET.response → extract value
 * 4. COSEM RLRQ → RLRE (release association)
 * 5. HDLC DISC → UA (disconnect data link)
 */

#ifndef DLMS_METER_H_
#define DLMS_METER_H_

#include <stdbool.h>
#include <stdint.h>

/* Meter connection state */
enum meter_state {
	METER_DISCONNECTED = 0,
	METER_HDLC_CONNECTED,     /* SNRM/UA done */
	METER_ASSOCIATED,         /* AARQ/AARE done */
	METER_ERROR,
};

/* Meter readings — all values in engineering units */
struct meter_readings {
	/* Per-phase voltages (V) */
	double voltage_r;
	double voltage_s;
	double voltage_t;

	/* Per-phase currents (A) */
	double current_r;
	double current_s;
	double current_t;

	/* Per-phase active power (kW) */
	double active_power_r;
	double active_power_s;
	double active_power_t;

	/* Per-phase reactive power (kvar) */
	double reactive_power_r;
	double reactive_power_s;
	double reactive_power_t;

	/* Per-phase apparent power (kVA) */
	double apparent_power_r;
	double apparent_power_s;
	double apparent_power_t;

	/* Per-phase power factor */
	double power_factor_r;
	double power_factor_s;
	double power_factor_t;

	/* Totals */
	double total_active_power;     /* kW */
	double total_reactive_power;   /* kvar */
	double total_apparent_power;   /* kVA */
	double total_power_factor;

	/* Energy */
	double active_energy;          /* kWh */
	double reactive_energy;        /* kvarh */
	double apparent_energy;        /* kVAh */

	/* Other */
	double frequency;              /* Hz */
	double neutral_current;        /* A */

	/* Metadata */
	bool   valid;                  /* True if at least some readings succeeded */
	int    read_count;             /* Number of successful OBIS reads */
	int    error_count;            /* Number of failed OBIS reads */
	int64_t timestamp_ms;          /* Uptime when readings were taken */
};

/* Meter configuration */
struct meter_config {
	uint8_t  client_sap;           /* Client logical address (default: 16) */
	uint8_t  server_logical;       /* Server logical address (default: 1) */
	uint8_t  server_physical;      /* Server physical address (default: 1) */
	char     password[16];         /* LLS password (default: "22222222") */
	uint16_t max_info_len;         /* Max HDLC info field (default: 128) */
	int      response_timeout_ms;  /* Response timeout (default: 5000) */
	int      inter_frame_delay_ms; /* Delay between frames (default: 100) */
};

/**
 * @brief Initialize the DLMS meter reader
 *
 * Initializes RS485 UART, sets default meter configuration.
 *
 * @return 0 on success, negative errno on failure
 */
int meter_init(void);

/**
 * @brief Set meter configuration
 *
 * @param cfg  Configuration structure (NULL resets to defaults)
 */
void meter_set_config(const struct meter_config *cfg);

/**
 * @brief Connect to the meter (HDLC + COSEM association)
 *
 * @return 0 on success, negative errno on failure
 */
int meter_connect(void);

/**
 * @brief Disconnect from the meter
 *
 * @return 0 on success, negative errno on failure
 */
int meter_disconnect(void);

/**
 * @brief Read all configured OBIS codes from the meter
 *
 * Must be connected (meter_connect) first.
 *
 * @param readings  Output structure for meter readings
 * @return 0 on success (at least partial), negative errno on total failure
 */
int meter_read_all(struct meter_readings *readings);

/**
 * @brief Full cycle: connect, read all, disconnect
 *
 * Convenience function for periodic polling. Handles errors gracefully.
 *
 * @param readings  Output structure for meter readings
 * @return 0 on success, negative errno on failure
 */
int meter_poll(struct meter_readings *readings);

/**
 * @brief Push meter readings to LwM2M Object 10242 resources
 *
 * Maps the meter_readings structure to LwM2M resources and notifies observers.
 *
 * @param readings  Meter readings to push
 */
void meter_push_to_lwm2m(const struct meter_readings *readings);

/**
 * @brief Get current meter state
 *
 * @return Current meter_state enum value
 */
enum meter_state meter_get_state(void);

#endif /* DLMS_METER_H_ */
