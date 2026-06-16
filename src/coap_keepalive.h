/*
 * CoAP-level keepalive (v0.6.67)
 *
 * Periodic notify on an existing observed resource so the engine emits a
 * confirmable CoAP message every N seconds, independent of REG_UPDATE
 * cadence and resource value changes. When the server socket dies (TB Edge
 * restart, mesh disruption, OTBR reboot), Zephyr's LwM2M engine detects
 * the CON timeout after 3 retries, fires lwm2m_rd_client_timeout(), and
 * forces a fresh REGISTER through the normal recover path. Without this,
 * a long client lifetime (e.g. 86400s, set to work around TB Edge's
 * REG_UPDATE/active tracking bug) means dead sockets aren't detected for
 * up to 2 days — the silence watchdog only fires at 2 * lifetime.
 */
#ifndef AMI_COAP_KEEPALIVE_H_
#define AMI_COAP_KEEPALIVE_H_

/* Start the keepalive thread. Call once after lwm2m engine init. */
void coap_keepalive_init(void);

/* Diag accessors (v0.6.68). */
#include <stdint.h>
uint32_t coap_keepalive_get_emit_count(void);
uint32_t coap_keepalive_get_consec_fail(void);

#endif /* AMI_COAP_KEEPALIVE_H_ */
