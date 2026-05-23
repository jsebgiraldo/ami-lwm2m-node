#ifndef LWM2M_OBJ_LIGHT_CONTROL_H
#define LWM2M_OBJ_LIGHT_CONTROL_H

/* Register IPSO Object 3311 (Light Control) instance 0 and wire the write
 * callbacks for /5850 On/Off, /5851 Dimmer, /5706 Colour. Must be called
 * AFTER lwm2m_obj_* initialisation order but before the LwM2M engine starts
 * its RD client (so the resources are present at registration). */
int light_control_init(void);

#endif /* LWM2M_OBJ_LIGHT_CONTROL_H */
