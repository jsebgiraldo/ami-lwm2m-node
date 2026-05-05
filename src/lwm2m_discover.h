/*
 * LwM2M server discovery via OpenThread DNS-SD / SRP.
 *
 * Tries to resolve the LwM2M server URI dynamically from the OTBR's
 * SRP/DNS-SD service. Falls back to compile-time Kconfig values if
 * discovery fails (timeout / no record).
 */

#ifndef LWM2M_DISCOVER_H_
#define LWM2M_DISCOVER_H_

#include <stddef.h>

/*
 * Resolve the LwM2M server URI via DNS-SD on Thread.
 *
 * Strategy (in order):
 *   1. otDnsClientResolveService("ThingsBoard-Edge",
 *                                "_lwm2m._udp.default.service.arpa.")
 *      → returns full coap://[<ip>]:<port>
 *   2. otDnsClientResolveAddress("thingsboard-edge.default.service.arpa.")
 *      → returns coap://[<ip>]:5683 (fixed default LwM2M port)
 *
 * Caller must hold a buffer >= 96 bytes for the URI.
 *
 * @return 0 on success, negative errno on failure.
 */
int lwm2m_discover_resolve(char *out_uri, size_t out_size, int timeout_ms);

#endif /* LWM2M_DISCOVER_H_ */
