/*
 * LwM2M IPSO Object 3311 (Light Control) — server-controlled RGB LED.
 *
 * Uses Zephyr's built-in ipso_light_control.c (enabled via
 * CONFIG_LWM2M_IPSO_LIGHT_CONTROL=y) for the object/resource scaffolding, and
 * registers post-write callbacks for the three resources we actually drive on
 * the AMI node's WS2812:
 *
 *   /3311/0/5850  On/Off   (bool)         — master switch
 *   /3311/0/5851  Dimmer   (int 0..100)   — brightness percent of full scale
 *   /3311/0/5706  Colour   (string)       — "#RRGGBB" hex OR name (red, green,
 *                                            blue, yellow, cyan, magenta, white,
 *                                            off)
 *
 * The final RGB applied is:  (r,g,b) * dimmer% * on_off, clamped to 0..255.
 *
 * Writes go through ami_led_set_raw() (main.c) which takes the same k_mutex
 * that serializes ami_set_rgb (v0.6.32 fix), so server-driven writes never
 * race the system status colors or the TX pulse.
 */
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#include "lwm2m_obj_light_control.h"

LOG_MODULE_REGISTER(light_control, LOG_LEVEL_INF);

#define LIGHT_OBJ_ID	3311
#define RES_ON_OFF	5850
#define RES_DIMMER	5851
#define RES_COLOUR	5706
/* RES_APP_TYPE (5750) intentionally not used — see init note. */

/* v0.6.42: LED OFF default. Operators report nodes feel hot; saving the ~3-5 mA
 * of continuous LED current cuts a couple of degrees and turns the LED into a
 * *fault indicator* — it lights up RED only when the previous reset was a
 * BROWNOUT (see light_control_set_brownout_indicator below). Everything OK = LED
 * stays dark. Server-driven writes via /3311 still override on demand. */
static bool light_on = false;
static uint8_t dimmer_pct = 0;
static uint8_t color_r = 0, color_g = 0, color_b = 0;
static char colour_buf[16] = "off";

/* v0.6.37: latches true on the first /3311 Write so main.c's ami_set_rgb()
 * stops fighting our apply_light() on every TX pulse / status change. */
static bool manual_mode = false;

bool light_control_manual_mode(void) { return manual_mode; }

extern void ami_led_set_raw(uint8_t r, uint8_t g, uint8_t b);

static void apply_light(void)
{
#ifdef CONFIG_AMI_LED_QUIET_MODE
	/* v0.6.51: production-quiet contract -- the LwM2M /3311 callbacks still
	 * accept server writes (so the object model stays correct and observable),
	 * but apply_light no-ops the physical write so the WS2812 stays dark
	 * regardless of TB Edge's persisted light_on / colour values. To re-enable
	 * server-driven LED control for diagnostics, rebuild with
	 * CONFIG_AMI_LED_QUIET_MODE=n. */
	LOG_INF("QUIET_MODE: apply_light no-op (on=%d dim=%u color=%s)",
		light_on, dimmer_pct, colour_buf);
	ami_led_set_raw(0, 0, 0);
	return;
#else
	if (!light_on || dimmer_pct == 0) {
		ami_led_set_raw(0, 0, 0);
		return;
	}
	uint32_t scale = dimmer_pct;
	uint8_t r = (color_r * scale) / 100;
	uint8_t g = (color_g * scale) / 100;
	uint8_t b = (color_b * scale) / 100;
	LOG_INF("Light apply: on=%d dim=%u%% color=(%u,%u,%u) -> (%u,%u,%u)",
		light_on, dimmer_pct, color_r, color_g, color_b, r, g, b);
	ami_led_set_raw(r, g, b);
#endif
}

struct named_color {
	const char *name;
	uint8_t r, g, b;
};
static const struct named_color NAMED[] = {
	{"off",     0,   0,   0  }, {"black",   0,   0,   0  },
	{"red",     255, 0,   0  }, {"green",   0,   255, 0  },
	{"blue",    0,   0,   255}, {"yellow",  255, 255, 0  },
	{"cyan",    0,   255, 255}, {"magenta", 255, 0,   255},
	{"white",   255, 255, 255},
};

static int parse_hex_pair(const char *s, uint8_t *out)
{
	int hi = (s[0] >= '0' && s[0] <= '9') ? s[0] - '0' :
		 (s[0] >= 'a' && s[0] <= 'f') ? s[0] - 'a' + 10 :
		 (s[0] >= 'A' && s[0] <= 'F') ? s[0] - 'A' + 10 : -1;
	int lo = (s[1] >= '0' && s[1] <= '9') ? s[1] - '0' :
		 (s[1] >= 'a' && s[1] <= 'f') ? s[1] - 'a' + 10 :
		 (s[1] >= 'A' && s[1] <= 'F') ? s[1] - 'A' + 10 : -1;
	if (hi < 0 || lo < 0) return -1;
	*out = (uint8_t)((hi << 4) | lo);
	return 0;
}

/* Accept "#RRGGBB", "RRGGBB", or a named color (case-insensitive). */
static int parse_color(const char *in, uint16_t len,
		       uint8_t *r, uint8_t *g, uint8_t *b)
{
	char buf[32];
	size_t n = MIN(len, sizeof(buf) - 1);
	for (size_t i = 0; i < n; i++) buf[i] = (char)tolower((unsigned char)in[i]);
	buf[n] = '\0';
	while (n > 0 && isspace((unsigned char)buf[n - 1])) buf[--n] = '\0';

	const char *p = buf;
	if (*p == '#') p++;
	if (strlen(p) == 6 &&
	    parse_hex_pair(p, r) == 0 &&
	    parse_hex_pair(p + 2, g) == 0 &&
	    parse_hex_pair(p + 4, b) == 0) {
		return 0;
	}
	for (size_t i = 0; i < ARRAY_SIZE(NAMED); i++) {
		if (strcmp(buf, NAMED[i].name) == 0) {
			*r = NAMED[i].r; *g = NAMED[i].g; *b = NAMED[i].b;
			return 0;
		}
	}
	return -1;
}

static int on_off_cb(uint16_t obj_inst_id, uint16_t res_id, uint16_t res_inst_id,
		     uint8_t *data, uint16_t data_len, bool last_block,
		     size_t total_size, size_t offset)
{
	light_on = (data_len > 0) && (*(uint8_t *)data != 0);
	manual_mode = true;
	LOG_INF("On/Off write -> %s (manual_mode=on)", light_on ? "ON" : "OFF");
	apply_light();
	return 0;
}

static int dimmer_cb(uint16_t obj_inst_id, uint16_t res_id, uint16_t res_inst_id,
		     uint8_t *data, uint16_t data_len, bool last_block,
		     size_t total_size, size_t offset)
{
	int32_t v = 0;
	if (data_len >= sizeof(int32_t)) {
		v = *(int32_t *)data;
	} else if (data_len > 0) {
		v = *(int8_t *)data;
	}
	if (v < 0) v = 0;
	if (v > 100) v = 100;
	dimmer_pct = (uint8_t)v;
	manual_mode = true;
	LOG_INF("Dimmer write -> %u%% (manual_mode=on)", dimmer_pct);
	apply_light();
	return 0;
}

static int colour_cb(uint16_t obj_inst_id, uint16_t res_id, uint16_t res_inst_id,
		     uint8_t *data, uint16_t data_len, bool last_block,
		     size_t total_size, size_t offset)
{
	uint8_t r, g, b;
	if (parse_color((const char *)data, data_len, &r, &g, &b) == 0) {
		color_r = r; color_g = g; color_b = b;
		size_t n = MIN(data_len, sizeof(colour_buf) - 1);
		memcpy(colour_buf, data, n);
		colour_buf[n] = '\0';
		manual_mode = true;
		LOG_INF("Colour write -> (%u,%u,%u) (manual_mode=on)", r, g, b);
		apply_light();
	} else {
		LOG_WRN("Colour write: unrecognised '%.*s'", data_len, (const char *)data);
	}
	return 0;
}

int light_control_init(void)
{
	int ret;
	ret = lwm2m_create_object_inst(&LWM2M_OBJ(LIGHT_OBJ_ID, 0));
	if (ret < 0) {
		LOG_ERR("Failed to create /3311/0 (%d)", ret);
		return ret;
	}
	/* Seed initial values so observers see something coherent.
	 *
	 * v0.6.36: do NOT lwm2m_set_string(/5750). Zephyr's ipso_light_control.c
	 * only allocates static buffers for /5706 (Colour, 64B) and /5701 (Units,
	 * 8B) — see LIGHT_STRING_LONG / LIGHT_STRING_SHORT. /5750 has NO buffer,
	 * so the set wrote to an uninitialised pointer → memory corruption →
	 * silent fault during MCUboot confirm window → revert (v0.6.35 bug, found
	 * via d2b4: state 1->2->0, fw stayed 0.6.33, total_resets bumped). */
	lwm2m_set_bool(&LWM2M_OBJ(LIGHT_OBJ_ID, 0, RES_ON_OFF), light_on);
	/* v0.6.38: do NOT seed /5851 — Zephyr's ipso_light_control allocates a
	 * 1-byte buffer for Dimmer and lwm2m_set_s32 writes 4 bytes -> harmless
	 * but noisy "Incorrect buffer length" error at boot. Our C variable
	 * dimmer_pct already defaults to 30 and is what apply_light reads. */
	lwm2m_set_string(&LWM2M_OBJ(LIGHT_OBJ_ID, 0, RES_COLOUR), colour_buf);

	lwm2m_register_post_write_callback(&LWM2M_OBJ(LIGHT_OBJ_ID, 0, RES_ON_OFF), on_off_cb);
	lwm2m_register_post_write_callback(&LWM2M_OBJ(LIGHT_OBJ_ID, 0, RES_DIMMER), dimmer_cb);
	lwm2m_register_post_write_callback(&LWM2M_OBJ(LIGHT_OBJ_ID, 0, RES_COLOUR), colour_cb);

	LOG_INF("Object 3311 Light Control initialised (on=%d dim=%u%% color=%s)",
		light_on, dimmer_pct, colour_buf);
	/* v0.6.42: do NOT apply_light at boot — defaults now OFF, leave LED dark
	 * unless an operator writes /3311 or main.c flags brownout via
	 * light_control_set_brownout_indicator(). */
	return 0;
}

/* v0.6.42: visible fault indicator — turn LED RED and lock manual_mode so the
 * system-status path can't overwrite. Operator can spot brownout-bound nodes
 * at a glance across the fleet (the only nodes with LED on = the failing ones).
 * Called from main.c after capture_reset_reason() if the previous reset
 * carried the BROWNOUT bit. Resets to OFF on a clean boot (defaults). */
void light_control_set_brownout_indicator(void)
{
	light_on = true;
	dimmer_pct = 30;
	color_r = 255; color_g = 0; color_b = 0;
	strncpy(colour_buf, "red", sizeof(colour_buf) - 1);
	colour_buf[sizeof(colour_buf) - 1] = '\0';
	manual_mode = true;
	apply_light();
	LOG_INF("BROWNOUT detected from previous reset -> LED RED indicator on");
}
