/*
 * LwM2M server discovery via OpenThread DNS-SD / SRP client.
 * See lwm2m_discover.h for behavior.
 */

#include "lwm2m_discover.h"

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include <openthread.h>
#include <openthread/instance.h>
#include <openthread/dns_client.h>
#include <openthread/ip6.h>
#include <openthread/thread.h>

LOG_MODULE_REGISTER(lwm2m_discover, LOG_LEVEL_INF);

#define LWM2M_DEFAULT_PORT  5683

#define SRV_INSTANCE_LABEL  "ThingsBoard-Edge"
#define SRV_TYPE_DOMAIN     "_lwm2m._udp.default.service.arpa."
#define HOST_FQDN           "thingsboard-edge.default.service.arpa."

struct discover_ctx {
	struct k_sem done;
	int err;
	otIp6Address addr;
	uint16_t port;
};

static void format_uri(char *out, size_t out_size,
		       const otIp6Address *addr, uint16_t port)
{
	char ip[OT_IP6_ADDRESS_STRING_SIZE];
	otIp6AddressToString(addr, ip, sizeof(ip));
	snprintf(out, out_size, "coap://[%s]:%u", ip, port);
}

/* True if `addr` falls inside the current Thread Mesh-Local /64 prefix.
 * Off-mesh-local addresses (e.g. OMR /64 published by the BR) are
 * preferred for LwM2M because the OTBR Linux host picks them as source
 * when sending replies; using a mesh-local destination causes a
 * src/dst prefix mismatch that breaks the connected UDP socket on the
 * client side.
 */
static bool addr_is_mesh_local(otInstance *inst, const otIp6Address *addr)
{
	const otMeshLocalPrefix *mlp = otThreadGetMeshLocalPrefix(inst);
	if (!mlp) {
		return false;
	}
	return memcmp(addr->mFields.m8, mlp->m8, sizeof(mlp->m8)) == 0;
}

static void on_service_resolved(otError err,
				const otDnsServiceResponse *resp,
				void *aCtx)
{
	struct discover_ctx *ctx = aCtx;

	if (err != OT_ERROR_NONE) {
		LOG_DBG("service resolve failed: %d", err);
		ctx->err = -ENOENT;
		k_sem_give(&ctx->done);
		return;
	}

	char host_buf[64];
	otDnsServiceInfo info = {
		.mHostNameBuffer = host_buf,
		.mHostNameBufferSize = sizeof(host_buf),
	};

	otError ge = otDnsServiceResponseGetServiceInfo(resp, &info);
	if (ge != OT_ERROR_NONE) {
		LOG_WRN("get_service_info failed: %d", ge);
		ctx->err = -EPROTO;
		k_sem_give(&ctx->done);
		return;
	}

	/* mHostAddress comes from the AAAA in the additional section; it may be
	 * the mesh-local one. We can't pick from multiple here, so accept it,
	 * but the caller should also try address-resolve to enumerate.
	 */
	ctx->addr = info.mHostAddress;
	ctx->port = info.mPort ? info.mPort : LWM2M_DEFAULT_PORT;
	ctx->err = 0;
	k_sem_give(&ctx->done);
}

static void on_address_resolved(otError err,
				const otDnsAddressResponse *resp,
				void *aCtx)
{
	struct discover_ctx *ctx = aCtx;

	if (err != OT_ERROR_NONE) {
		LOG_DBG("address resolve failed: %d", err);
		ctx->err = -ENOENT;
		k_sem_give(&ctx->done);
		return;
	}

	otInstance *inst = openthread_get_default_instance();
	otIp6Address fallback = {0};
	bool have_fallback = false;
	bool picked = false;

	for (uint16_t i = 0; i < 16; i++) {
		uint32_t ttl;
		otIp6Address a;
		otError ge = otDnsAddressResponseGetAddress(resp, i, &a, &ttl);
		if (ge != OT_ERROR_NONE) {
			break;
		}
		if (!have_fallback) {
			fallback = a;
			have_fallback = true;
		}
		if (!addr_is_mesh_local(inst, &a)) {
			ctx->addr = a;
			picked = true;
			char buf[OT_IP6_ADDRESS_STRING_SIZE];
			otIp6AddressToString(&a, buf, sizeof(buf));
			LOG_INF("preferred off-mesh-local address: %s", buf);
			break;
		}
	}

	if (!picked) {
		if (!have_fallback) {
			ctx->err = -EPROTO;
			k_sem_give(&ctx->done);
			return;
		}
		ctx->addr = fallback;
		char buf[OT_IP6_ADDRESS_STRING_SIZE];
		otIp6AddressToString(&fallback, buf, sizeof(buf));
		LOG_WRN("only mesh-local address available: %s "
			"(may break src/dst symmetry on BR)", buf);
	}

	ctx->port = LWM2M_DEFAULT_PORT;
	ctx->err = 0;
	k_sem_give(&ctx->done);
}

int lwm2m_discover_resolve(char *out_uri, size_t out_size, int timeout_ms)
{
	otInstance *inst = openthread_get_default_instance();
	if (!inst) {
		return -ENODEV;
	}

	struct discover_ctx ctx;
	k_sem_init(&ctx.done, 0, 1);

	/* Strategy 1: full SRV/TXT resolution → address + port */
	ctx.err = -EAGAIN;
	openthread_mutex_lock();
	otError oe = otDnsClientResolveService(inst,
					       SRV_INSTANCE_LABEL,
					       SRV_TYPE_DOMAIN,
					       on_service_resolved,
					       &ctx,
					       /* default config */ NULL);
	openthread_mutex_unlock();

	if (oe == OT_ERROR_NONE &&
	    k_sem_take(&ctx.done, K_MSEC(timeout_ms)) == 0 &&
	    ctx.err == 0) {
		format_uri(out_uri, out_size, &ctx.addr, ctx.port);
		LOG_INF("DNS-SD service resolved: %s", out_uri);
		return 0;
	}

	LOG_INF("service resolve unavailable, trying host A/AAAA");

	/* Strategy 2: host AAAA only → fixed default LwM2M port */
	k_sem_reset(&ctx.done);
	ctx.err = -EAGAIN;
	openthread_mutex_lock();
	oe = otDnsClientResolveAddress(inst,
				       HOST_FQDN,
				       on_address_resolved,
				       &ctx,
				       NULL);
	openthread_mutex_unlock();

	if (oe == OT_ERROR_NONE &&
	    k_sem_take(&ctx.done, K_MSEC(timeout_ms)) == 0 &&
	    ctx.err == 0) {
		format_uri(out_uri, out_size, &ctx.addr, ctx.port);
		LOG_INF("DNS host resolved: %s", out_uri);
		return 0;
	}

	LOG_WRN("DNS-SD discovery failed — caller should fall back to Kconfig");
	return -ENOENT;
}
