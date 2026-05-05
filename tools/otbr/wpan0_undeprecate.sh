#!/bin/sh
# OpenWrt hotplug.d/iface action: un-deprecate the Thread mesh-local EID on
# wpan0 so the kernel picks it as source for replies to mesh-local destinations.
#
# Without this, otbr-agent leaves all wpan0 mesh-local addresses with
# preferred_lft=0 (deprecated). Linux RFC 6724 rule 3 then avoids them and
# uses the OMR address as source — which breaks the UDP peer match in
# Zephyr LwM2M clients connect()-ed to the mesh-local server EID.
#
# Install path: /etc/hotplug.d/iface/99-wpan0-undeprecate
#
# Symptoms when missing: TB Edge shows ObserveRequest TimeoutException for
# every observed resource; node is Active but no sensor telemetry arrives.

[ "$INTERFACE" = "wpan0" ] || [ -z "$INTERFACE" ] || exit 0
[ "$ACTION" = "ifup" ] || [ -z "$ACTION" ] || exit 0

# Wait briefly for otbr-agent to assign addresses
sleep 1

EID=$(ot-ctl ipaddr mleid 2>/dev/null | head -1 | tr -d '\r')
[ -n "$EID" ] || { logger -t wpan0-undeprecate "no mesh-local EID found"; exit 0; }

ip -6 addr change "${EID}/64" dev wpan0 preferred_lft forever valid_lft forever \
    && logger -t wpan0-undeprecate "un-deprecated ${EID} on wpan0" \
    || logger -t wpan0-undeprecate "ip addr change failed for ${EID}"
