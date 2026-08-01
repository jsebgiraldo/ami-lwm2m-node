#ifndef COAP_DIAG_H
#define COAP_DIAG_H

/* Object-independent, on-demand IPv6 diagnostics.
 *
 * Starts a tiny CoAP server (Zephyr coap_service) that answers a GET to
 *   coap://[<node-ipv6>]:5685/diag
 * with a JSON snapshot of the node's live state (role, uptime, resets,
 * registration counters, partition, rloc16). Works over the node's Thread
 * IPv6 address and is INDEPENDENT of LwM2M/CoAP-observe — so the server can
 * query a node directly even when it is not registered on TB.
 *
 * Call once after OpenThread is initialised; the service auto-starts when the
 * network comes up. Pass the firmware version string so it is reported.
 */
int coap_diag_init(const char *fw_version);

#endif /* COAP_DIAG_H */
