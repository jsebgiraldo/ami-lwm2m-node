/*
 * Stub: zephyr/net/lwm2m.h
 * No-op replacements for LwM2M engine functions used in dlms_meter.c
 */
#ifndef ZEPHYR_NET_LWM2M_H_
#define ZEPHYR_NET_LWM2M_H_

#include <stdint.h>
#include <stddef.h>

/* Path struct used by observation API */
struct lwm2m_obj_path {
	uint16_t obj_id;
	uint16_t obj_inst_id;
	uint16_t res_id;
	uint16_t res_inst_id;
	uint8_t  level;
};

/* LWM2M_OBJ macro — creates a compound literal path */
#define LWM2M_OBJ(obj_id, obj_inst_id, res_id) \
	((struct lwm2m_obj_path){ (obj_id), (obj_inst_id), (res_id), 0, 3 })

/* Stub implementations — accept path pointer, do nothing */
static inline int lwm2m_set_f64(const struct lwm2m_obj_path *path, double value)
{
	(void)path; (void)value;
	return 0;
}

static inline int lwm2m_set_string(const struct lwm2m_obj_path *path, const char *value, uint16_t len)
{
	(void)path; (void)value; (void)len;
	return 0;
}

static inline int lwm2m_set_u32(const struct lwm2m_obj_path *path, uint32_t value)
{
	(void)path; (void)value;
	return 0;
}

static inline int lwm2m_set_s32(const struct lwm2m_obj_path *path, int32_t value)
{
	(void)path; (void)value;
	return 0;
}

#endif /* ZEPHYR_NET_LWM2M_H_ */
