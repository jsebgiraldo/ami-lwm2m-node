#!/bin/sh
# srp_publish_lwm2m.sh — register the bench ThingsBoard LwM2M server into the
# local OTBR's Thread SRP server, so the AMI node's DNS-SD discovery resolves.
#
# WHY
#   src/lwm2m_discover.c is the node's ONLY way to find a server (the static
#   CONFIG_AMI_LWM2M_SERVER_IPV6_* fallback was removed in v0.6.65). It looks up
#       ThingsBoard-Edge._lwm2m._udp.default.service.arpa.   (SRV -> addr+port)
#       thingsboard-edge.default.service.arpa.               (AAAA, port 5683)
#   against the OTBR's SRP/DNS-SD server. Nothing publishes those records on a
#   bench, so the node retries 10x and reboots. This script publishes them.
#
# HOW
#   ThingsBoard runs on the WSL host, which is not a Thread node and so cannot
#   run an SRP client of its own. But the OTBR's *own* OpenThread instance can:
#   we drive its SRP client to register host `thingsboard-edge` -> the wpan0
#   OMR address (which genuinely belongs to the machine running ThingsBoard),
#   plus service `ThingsBoard-Edge._lwm2m._udp` on port 5683. The record then
#   lives in `ot-ctl srp server service` — exactly where production has it and
#   where tools/edge_health.py looks for it.
#
#   The OMR (off-mesh-local) address is chosen deliberately: it is what Linux
#   picks as the source when replying to mesh traffic, and it is what
#   lwm2m_discover.c:113-131 prefers. Advertising a mesh-local address instead
#   reproduces the 2026-06-04 src/dst-mismatch outage.
#
# WHERE
#   Installed to /usr/local/sbin/srp_publish_lwm2m.sh and driven by
#   otbr-srp-lwm2m.service (see tools/lab_tb/). Install both with:
#       python tools/lab_tb/lab_tb_srp.py install
#
# USAGE
#   srp_publish_lwm2m.sh publish   # one-shot: wait for attach, register, verify
#   srp_publish_lwm2m.sh verify    # checks only; exit 1 if the node can't resolve
#   srp_publish_lwm2m.sh remove    # unregister (releases name + key lease)
#   srp_publish_lwm2m.sh address   # print the IPv6 that would be advertised
#   srp_publish_lwm2m.sh daemon    # publish, then re-assert every RECHECK_S
#
# ENV OVERRIDES
#   OTCTL          how to reach ot-ctl        (default: ot-ctl)
#                  e.g. OTCTL="docker exec otbr ot-ctl" for a containerised OTBR
#   INSTANCE       SRV instance label         (default: ThingsBoard-Edge)
#   SERVICE        service type               (default: _lwm2m._udp)
#   HOST_LABEL     SRP host label             (default: thingsboard-edge)
#   DOMAIN         DNS-SD domain              (default: default.service.arpa.)
#   LWM2M_PORT     port TB binds LwM2M on     (default: 5683)
#   ADDR           advertise this IPv6 instead of auto-picking the OMR address
#   WAIT_ATTACH_S  how long to wait for Thread attach   (default: 300)
#   WAIT_REG_S     how long to wait for host Registered (default: 60)
#   RECHECK_S      daemon re-assert interval  (default: 120)
#
# POSIX sh. No bashisms, no external deps beyond awk/sed/grep — it has to run
# on a minimal OTBR host as well as on WSL Ubuntu.

set -u

OTCTL="${OTCTL:-ot-ctl}"
INSTANCE="${INSTANCE:-ThingsBoard-Edge}"
SERVICE="${SERVICE:-_lwm2m._udp}"
HOST_LABEL="${HOST_LABEL:-thingsboard-edge}"
DOMAIN="${DOMAIN:-default.service.arpa.}"
LWM2M_PORT="${LWM2M_PORT:-5683}"
ADDR="${ADDR:-}"
WAIT_ATTACH_S="${WAIT_ATTACH_S:-300}"
WAIT_REG_S="${WAIT_REG_S:-60}"
RECHECK_S="${RECHECK_S:-120}"

HOST_FQDN="${HOST_LABEL}.${DOMAIN}"
SVC_FQDN="${SERVICE}.${DOMAIN}"
INST_FQDN="${INSTANCE}.${SVC_FQDN}"

log() { echo "[srp-publish] $*" >&2; }

# Run ot-ctl, strip CR and the trailing "Done". $OTCTL is intentionally
# unquoted so it can carry a multi-word prefix (docker exec otbr ot-ctl).
# shellcheck disable=SC2086
ot() { $OTCTL "$@" 2>&1 | tr -d '\r'; }

# True when the output had no "Error N:" line.
ot_ok() { ot "$@" | grep -q '^Error [0-9]' && return 1 || return 0; }

first_line() { ot "$@" | grep -v '^Done$' | grep -v '^[[:space:]]*$' | head -n 1; }

# ── Thread / SRP server preconditions ───────────────────────────────────────
wait_attached() {
    _end=$(( $(date +%s) + WAIT_ATTACH_S ))
    while [ "$(date +%s)" -lt "$_end" ]; do
        _st=$(first_line state)
        case "$_st" in
            leader|router|child) log "Thread state=$_st"; return 0 ;;
        esac
        sleep 3
    done
    log "FAIL Thread never attached within ${WAIT_ATTACH_S}s (state=$(first_line state))"
    return 1
}

ensure_srp_server() {
    _st=$(first_line srp server state)
    case "$_st" in
        *running*) return 0 ;;
    esac
    log "SRP server state=${_st:-?} -> enabling"
    ot srp server enable >/dev/null 2>&1
    _end=$(( $(date +%s) + 15 ))
    while [ "$(date +%s)" -lt "$_end" ]; do
        _st=$(first_line srp server state)
        case "$_st" in *running*) return 0 ;; esac
        sleep 1
    done
    log "FAIL SRP server did not reach 'running' (state=${_st:-?})"
    return 1
}

# ── Address selection (mirrors lwm2m_discover.c's own preference) ───────────
# 1. an address inside the BR's OMR prefix
# 2. else any address that is neither link-local nor mesh-local
# 3. else refuse — a mesh-local advert is the 2026-06-04 src/dst-mismatch trap
pick_address() {
    [ -n "$ADDR" ] && { echo "$ADDR"; return 0; }

    # "Local: fdaf:e549:1751:1::/64" / "Favored: fdaf:e549:1751:1::/64 prf:low"
    _pfx=$(ot br omrprefix | awk '
        /^Favored:/ { fav=$2 }
        /^Local:/   { loc=$2 }
        END { if (fav != "") print fav; else if (loc != "") print loc }')
    # fdaf:e549:1751:1::/64 -> fdaf:e549:1751:1:   (ot-ctl never "::"-compresses
    # the address side, so a literal prefix match is safe here)
    _match=$(echo "$_pfx" | sed 's|::/[0-9]*$|:|')

    _mleid=$(first_line ipaddr mleid)
    _ml=$(echo "$_mleid" | cut -d: -f1-4)

    _addrs=$(ot ipaddr | grep -v '^Done$' | grep ':' | grep -v '^Error')

    if [ -n "$_match" ] && [ "$_match" != "$_pfx" ]; then
        for _a in $_addrs; do
            case "$_a" in "$_match"*) echo "$_a"; return 0 ;; esac
        done
    fi

    for _a in $_addrs; do
        case "$_a" in
            fe80:*) continue ;;
            "${_ml}":*) continue ;;
        esac
        echo "$_a"; return 0
    done

    log "FAIL no off-mesh-local (OMR) address on the Thread interface."
    log "     'ot-ctl br state' must be 'running' and 'ot-ctl br omrprefix'"
    log "     must show an fd..::/64 before anything can be published."
    return 1
}

# ── Registry inspection ─────────────────────────────────────────────────────
# record_block <ot-ctl subcommand...> <fqdn> — print one indented record block.
# ot-ctl prints the record name unindented and its fields indented, so the
# block ends at the next unindented line (including the trailing "Done").
record_block() {
    _n=$1; shift
    ot "$@" | awk -v n="$_n" '
        $0 == n { inblock=1; print; next }
        inblock && /^[^[:space:]]/ { inblock=0 }
        inblock { print }'
}

service_block() { record_block "$INST_FQDN" srp server service; }
host_block()    { record_block "$HOST_FQDN" srp server host; }

# published_ok <address> — true when the registry already holds exactly the
# record we want. The port/deleted flags come from the service record and the
# address from the host record (the service block does not always echo the
# host's addresses). Both sides of the address comparison come from ot-ctl,
# which never "::"-compresses, so a literal string match is sound.
published_ok() {
    _svc=$(service_block)
    [ -z "$_svc" ] && return 1
    echo "$_svc" | grep -qF 'deleted: false' || return 1
    echo "$_svc" | grep -q "port: ${LWM2M_PORT}\$" || return 1
    _hst=$(host_block)
    [ -z "$_hst" ] && return 1
    echo "$_hst" | grep -qF 'deleted: false' || return 1
    echo "$_hst" | grep -qF "$1" || return 1
    return 0
}

# ── Publish / remove ────────────────────────────────────────────────────────
do_publish() {
    _addr=$(pick_address) || return 1
    log "advertising [$_addr]:${LWM2M_PORT} as ${INST_FQDN}"

    if published_ok "$_addr"; then
        log "already published (no-op)"
        return 0
    fi

    # Clean slate. 'stop' + 'host clear' resets the CLIENT's view WITHOUT
    # touching the persisted ECDSA key (OpenThread keeps it in settings), so the
    # re-registration re-uses the same KEY and the server treats it as an update
    # of the same name rather than a name conflict. Errors on a first run are
    # expected and ignored.
    ot srp client stop           >/dev/null 2>&1
    ot srp client service clear  >/dev/null 2>&1
    ot srp client host clear     >/dev/null 2>&1

    if ! ot_ok srp client host name "$HOST_LABEL"; then
        log "FAIL 'srp client host name' rejected:"
        ot srp client host name "$HOST_LABEL" | sed 's/^/       /' >&2
        log "     If this says InvalidCommand, otbr-agent was built without"
        log "     OT_SRP_CLIENT=ON -> use the avahi fallback instead"
        log "     (python tools/lab_tb/lab_tb_srp.py install-avahi)."
        return 1
    fi
    ot_ok srp client host address "$_addr" || { log "FAIL host address $_addr"; return 1; }
    ot_ok srp client service add "$INSTANCE" "$SERVICE" "$LWM2M_PORT" || {
        log "FAIL service add $INSTANCE $SERVICE $LWM2M_PORT"; return 1; }
    ot_ok srp client autostart enable || { log "FAIL autostart enable"; return 1; }

    _end=$(( $(date +%s) + WAIT_REG_S ))
    while [ "$(date +%s)" -lt "$_end" ]; do
        case "$(first_line srp client host state)" in
            Registered*) log "registered ${HOST_FQDN} -> [$_addr]"; return 0 ;;
        esac
        sleep 2
    done

    log "FAIL host state=$(first_line srp client host state) after ${WAIT_REG_S}s (want Registered)"
    log "     SRP server the client selected: $(first_line srp client server)"
    log "     If it never leaves ToAdd, autostart did not find the local SRP"
    log "     server; start it explicitly:  ot-ctl srp client start <addr> <port>"
    return 1
}

do_remove() {
    ot srp client host remove 1 1 >/dev/null 2>&1
    _end=$(( $(date +%s) + 15 ))
    while [ "$(date +%s)" -lt "$_end" ]; do
        case "$(first_line srp client host state)" in
            Removed*|"") break ;;
        esac
        sleep 1
    done
    ot srp client service clear >/dev/null 2>&1
    ot srp client host clear    >/dev/null 2>&1
    ot srp client stop          >/dev/null 2>&1
    log "unregistered ${INST_FQDN}"
}

# ── Verification — proves a Thread node can resolve it ──────────────────────
do_verify() {
    _rc=0
    _addr=$(pick_address 2>/dev/null)

    _hst=$(host_block)
    if [ -n "$_hst" ] && echo "$_hst" | grep -qF 'deleted: false'; then
        echo "[ OK ] srp server host   : $HOST_FQDN -> $(echo "$_hst" | grep -F 'addresses:' | tr -s ' ')"
    else
        echo "[FAIL] srp server host   : $HOST_FQDN missing or deleted"; _rc=1
    fi

    _blk=$(service_block)
    if [ -n "$_blk" ] && echo "$_blk" | grep -q "port: ${LWM2M_PORT}\$"; then
        echo "[ OK ] srp server service: $INST_FQDN port $LWM2M_PORT"
    else
        echo "[FAIL] srp server service: $INST_FQDN missing or wrong port"; _rc=1
    fi

    # These two run the OTBR's own DNS client against its DNS-SD server — the
    # exact otDnsClientResolveService / otDnsClientResolveAddress calls the
    # firmware makes, minus the radio hop.
    _svc=$(ot dns service "$INSTANCE" "$SVC_FQDN")
    if echo "$_svc" | grep -q "Port:${LWM2M_PORT}"; then
        echo "[ OK ] dns service       : $(echo "$_svc" | grep -i 'Port:' | head -1)"
    else
        echo "[FAIL] dns service       : $(echo "$_svc" | head -2 | tr '\n' ' ')"; _rc=1
    fi

    _res=$(ot dns resolve "$HOST_FQDN")
    if echo "$_res" | grep -qi 'DNS response'; then
        echo "[ OK ] dns resolve       : $(echo "$_res" | head -1)"
    else
        echo "[FAIL] dns resolve       : $(echo "$_res" | head -2 | tr '\n' ' ')"; _rc=1
    fi

    [ -n "$_addr" ] && echo "       advertised address: $_addr"
    return $_rc
}

# ── main ────────────────────────────────────────────────────────────────────
case "${1:-publish}" in
    address)
        pick_address ;;
    verify)
        do_verify ;;
    remove)
        do_remove ;;
    publish)
        wait_attached && ensure_srp_server && do_publish && do_verify ;;
    daemon)
        # Re-assert forever: survives Thread detach/re-attach and an OMR prefix
        # change. The SRP registration lives in RAM on the OTBR, so anything
        # that restarts otbr-agent silently drops the node's server.
        wait_attached || exit 1
        ensure_srp_server || exit 1
        do_publish || log "initial publish failed; will keep retrying"
        while :; do
            sleep "$RECHECK_S"
            _a=$(pick_address 2>/dev/null) || continue
            if ! published_ok "$_a"; then
                log "record missing or stale -> re-publishing"
                ensure_srp_server && do_publish
            fi
        done ;;
    *)
        echo "usage: $0 {publish|verify|remove|address|daemon}" >&2
        exit 2 ;;
esac
