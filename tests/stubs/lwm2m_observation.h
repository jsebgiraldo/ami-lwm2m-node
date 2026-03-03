/*
 * Stub: lwm2m_observation.h
 * No-op replacements for LwM2M observation functions.
 */
#ifndef LWM2M_OBSERVATION_H
#define LWM2M_OBSERVATION_H

#include <stdint.h>

static inline int lwm2m_notify_observer(uint16_t obj_id, uint16_t obj_inst_id, uint16_t res_id)
{
	(void)obj_id; (void)obj_inst_id; (void)res_id;
	return 0;
}

#endif /* LWM2M_OBSERVATION_H */
