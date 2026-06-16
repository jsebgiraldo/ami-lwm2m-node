#ifndef LWM2M_OBJ_LIGHT_CONTROL_H
#define LWM2M_OBJ_LIGHT_CONTROL_H

#include <stdbool.h>

/* Register IPSO Object 3311 (Light Control) instance 0 and wire the write
 * callbacks for /5850 On/Off, /5851 Dimmer, /5706 Colour. Must be called
 * AFTER lwm2m_obj_* initialisation order but before the LwM2M engine starts
 * its RD client (so the resources are present at registration). */
int light_control_init(void);

/* v0.6.37: returns true once ANY /3311 Write has come in, signalling the
 * server now owns the LED. main.c's ami_set_rgb() consults this flag and
 * SKIPS the WS2812 update when manual mode is active, so system status
 * colours (registration/role/TX pulse) stop overwriting the server-driven
 * colour every TX cycle. Cleared only by reboot (factory state = automatic). */
bool light_control_manual_mode(void);

/* v0.6.42: set LED to RED + lock manual_mode. Called by main.c when the
 * previous boot ended in a BROWNOUT reset, so the operator can identify
 * failing nodes visually across the fleet. LED off otherwise. */
void light_control_set_brownout_indicator(void);

#endif /* LWM2M_OBJ_LIGHT_CONTROL_H */
