#!/usr/bin/env bash
#
# lab_tb_up.sh — bring up / verify / tear down the BENCH ThingsBoard CE server
#                that gives the LAB Thread mesh a real LwM2M endpoint.
#
# WHY
#   The lab node (ami-esp32c6-3bb0) resolves its LwM2M server ONLY through
#   DNS-SD over Thread and then REGISTERs to it. Without a server the bench is
#   forced to run overlays/lab.conf's serverless watchdog values
#   (CONFIG_AMI_BOOT_REGISTER_DEADLINE_S=0, HW_WATCHDOG_BOOT_GRACE_HARD_S=3600),
#   which we would never ship — so every bench result is unrepresentative.
#   This script stands up the same product the fleet runs (ThingsBoard) inside
#   WSL2, on the same network namespace as the native otbr-agent, so the LwM2M
#   socket lives on wpan0's OMR address and the node can actually register.
#
# WHERE IT RUNS
#   Inside WSL (Ubuntu-24.04), as root (docker.sock + `ss -p` both want it).
#   From Windows use the wrapper: tools/lab_tb/lab_tb.ps1 -Action <action>
#
# USAGE (inside WSL)
#   LAB_TB_DIR=/mnt/c/.../tools/lab_tb bash lab_tb_up.sh <action>
#
#   preflight   docker present? RAM? are 8080/5683/5432 free? is wpan0 up?
#   pull        docker compose pull (postgres is usually the only new image)
#   install     ONE-TIME DB schema + demo data (creates tenant@thingsboard.org)
#   up          start the stack and block until TB answers on HTTP + UDP
#   verify      the acceptance gate: bind family/port, log line, REST login
#   status      containers + listening sockets + wpan0 addresses
#   logs        tail TB's log (add -f via LAB_TB_FOLLOW=1)
#   restart     restart TB only (needed after every LwM2M model upload)
#   down        stop the stack, KEEP the database
#   reset       stop + DELETE the database volume (requires LAB_TB_YES=1)
#   srpinfo     print the exact SRP host/service the node needs published
#
# DEGRADES GRACEFULLY: every probe that needs an optional tool (curl, ss) says
# so and keeps going instead of aborting the run.

set -uo pipefail

# ── Locate ourselves ───────────────────────────────────────────────────────
# LAB_TB_DIR is set by lab_tb.ps1 (which pipes this file through `sed` to strip
# CRLF, so $BASH_SOURCE may not be a real path). Fall back to dirname.
if [ -n "${LAB_TB_DIR:-}" ]; then
    HERE="$LAB_TB_DIR"
else
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
COMPOSE_FILE="$HERE/docker-compose.yml"
PROJECT="lab-tb"

TB_CONTAINER="tb-lab"
PG_CONTAINER="tb-lab-postgres"
PG_VOLUME="tb-lab-postgres-data"

# Keep these in sync with docker-compose.yml defaults. An .env next to the
# compose file wins for the containers; we re-read it here so the probes look at
# the same numbers the stack actually used.
HTTP_PORT=8080
LWM2M_PORT=5683
PG_PORT=5432
if [ -f "$HERE/.env" ]; then
    # shellcheck disable=SC1090,SC1091
    HTTP_PORT="$(sed -n 's/^TB_HTTP_PORT=//p'   "$HERE/.env" | tr -d '\r' | tail -1 || true)"
    LWM2M_PORT="$(sed -n 's/^TB_LWM2M_PORT=//p' "$HERE/.env" | tr -d '\r' | tail -1 || true)"
    PG_PORT="$(sed -n 's/^TB_PG_PORT=//p'       "$HERE/.env" | tr -d '\r' | tail -1 || true)"
    HTTP_PORT="${HTTP_PORT:-8080}"; LWM2M_PORT="${LWM2M_PORT:-5683}"; PG_PORT="${PG_PORT:-5432}"
fi

TENANT_USER="tenant@thingsboard.org"
TENANT_PASS="tenant"

# ── Output helpers ─────────────────────────────────────────────────────────
say()  { printf '%s\n' "$*"; }
hdr()  { printf '\n== %s ==\n' "$*"; }
ok()   { printf '  [ OK ] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

dc() { docker compose -f "$COMPOSE_FILE" -p "$PROJECT" --project-directory "$HERE" "$@"; }

require_env() {
    [ -f "$COMPOSE_FILE" ] || die "compose file not found: $COMPOSE_FILE (set LAB_TB_DIR)"
    have docker || die "docker not on PATH inside WSL. Native docker is expected at /var/run/docker.sock."
    docker version >/dev/null 2>&1 || die "cannot talk to the docker daemon (run as root: wsl -d Ubuntu-24.04 -u root -- ...)"
    docker compose version >/dev/null 2>&1 || die "the docker compose v2 plugin is missing (docker-compose v1 is not supported)."
}

# ── Probes ─────────────────────────────────────────────────────────────────

# port_holders <tcp|udp> <port> -> lines from ss, empty if free
port_holders() {
    local proto="$1" port="$2"
    have ss || { echo "__NOSS__"; return; }
    if [ "$proto" = udp ]; then
        ss -H -lunp "sport = :$port" 2>/dev/null
    else
        ss -H -ltnp "sport = :$port" 2>/dev/null
    fi
}

check_port_free() {
    local proto="$1" port="$2" label="$3" out
    out="$(port_holders "$proto" "$port")"
    if [ "$out" = "__NOSS__" ]; then
        warn "iproute2 'ss' missing — cannot check $proto/$port ($label). apt-get install -y iproute2"
        return 0
    fi
    if [ -n "$out" ]; then
        bad "$proto/$port ($label) is ALREADY IN USE:"
        printf '%s\n' "$out" | sed 's/^/         /'
        return 1
    fi
    ok "$proto/$port ($label) free"
    return 0
}

wpan0_addrs() {
    ip -6 addr show dev wpan0 scope global 2>/dev/null | awk '/inet6/ {print $2}' | cut -d/ -f1
}

# The OMR address = the one the mesh routes to (NOT the mesh-local fdfe:/fd..:0:ff:fe00 ones).
omr_addr() {
    local a
    for a in $(wpan0_addrs); do
        case "$a" in
            *:0:ff:fe00:*) continue ;;   # RLOC / ALOC
        esac
        # mesh-local prefix on this bench is fdfe:139:817c:7d90::/64
        case "$a" in
            fdfe:*) continue ;;
        esac
        printf '%s\n' "$a"; return 0
    done
    return 1
}

tb_running() { [ "$(docker inspect -f '{{.State.Running}}' "$TB_CONTAINER" 2>/dev/null)" = "true" ]; }

http_code() {
    local url="$1"
    if have curl; then
        curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" 2>/dev/null
    else
        # /dev/tcp fallback: prove the port accepts a connection, no status code
        local hostport="${url#http://}"; hostport="${hostport%%/*}"
        if (exec 3<>"/dev/tcp/${hostport%%:*}/${hostport##*:}") 2>/dev/null; then echo "connect"; else echo "000"; fi
    fi
}

wait_ready() {
    local timeout="${1:-300}" t=0 code
    say "  waiting for TB on http://127.0.0.1:$HTTP_PORT (timeout ${timeout}s; first boot is the slow one)"
    while [ "$t" -lt "$timeout" ]; do
        code="$(http_code "http://127.0.0.1:$HTTP_PORT/login")"
        case "$code" in
            200|302|401|connect) ok "HTTP up after ${t}s (code=$code)"; return 0 ;;
        esac
        if ! tb_running; then bad "container $TB_CONTAINER exited while starting"; dc logs --tail 60 thingsboard-ce; return 1; fi
        sleep 5; t=$((t+5))
        [ $((t % 30)) -eq 0 ] && say "    ... ${t}s"
    done
    bad "TB did not answer within ${timeout}s"
    return 1
}

db_installed() {
    docker exec "$PG_CONTAINER" psql -U postgres -d thingsboard -tAc \
        "select count(*) from tb_schema_settings" >/dev/null 2>&1
}

# ── Actions ────────────────────────────────────────────────────────────────

do_preflight() {
    require_env
    local rc=0

    hdr "1/5 docker"
    ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?') / compose $(docker compose version --short 2>/dev/null || echo '?')"

    hdr "2/5 images"
    local img
    for img in "thingsboard/tb-node" "postgres"; do
        if docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -q "^$img:"; then
            ok "present: $(docker image ls --format '{{.Repository}}:{{.Tag}} ({{.Size}})' | grep "^$img:" | head -3 | tr '\n' ' ')"
        else
            warn "$img not pulled yet — 'pull' will fetch it"
        fi
    done

    hdr "3/5 memory"
    if have free; then
        local total_mb; total_mb="$(free -m | awk '/^Mem:/{print $2}')"
        say "  WSL total RAM: ${total_mb} MB"
        if [ "${total_mb:-0}" -lt 3072 ]; then
            warn "under 3 GB — lower TB_JAVA_XMX in .env (see .env.example) or raise memory= in .wslconfig"
        else
            ok "enough headroom for -Xmx2048m + postgres"
        fi
    else
        warn "'free' unavailable — cannot size-check"
    fi

    hdr "4/5 ports (must be free BEFORE 'up')"
    check_port_free tcp "$HTTP_PORT" "TB web/REST"   || rc=1
    check_port_free udp "$LWM2M_PORT" "LwM2M NoSec"  || rc=1
    check_port_free tcp "$PG_PORT"   "postgres"      || rc=1
    check_port_free udp 5684 "LwM2M DTLS"            || rc=1
    say "  (if 5683/udp is held by an old tb-lab, run './lab_tb_up.sh down' first)"

    hdr "5/5 Thread side"
    local addrs; addrs="$(wpan0_addrs)"
    if [ -z "$addrs" ]; then
        bad "wpan0 has no global IPv6 address — the OTBR is not up. Nothing to serve."
        rc=1
    else
        say "  wpan0 global addresses:"; printf '%s\n' "$addrs" | sed 's/^/         /'
        local omr; omr="$(omr_addr)" && ok "OMR (advertise THIS in SRP): $omr" || warn "no off-mesh-local address found; check 'ot-ctl br omrprefix'"
    fi

    say ""
    if [ "$rc" -eq 0 ]; then
        say "Preflight PASSED — next: ./lab_tb_up.sh install"
    else
        say "Preflight FAILED — fix the [FAIL] lines above first."
    fi
    return "$rc"
}

do_pull() {
    require_env
    hdr "docker compose pull"
    say "  tb-node:4.3.1.3 should already be local; postgres is the ~150 MB fetch."
    dc pull
}

do_install() {
    require_env
    hdr "install: database schema + system assets"

    say "  starting postgres and waiting for it to be healthy..."
    dc up -d postgres || die "postgres failed to start"
    local t=0
    while [ "$t" -lt 120 ]; do
        [ "$(docker inspect -f '{{.State.Health.Status}}' "$PG_CONTAINER" 2>/dev/null)" = "healthy" ] && break
        sleep 5; t=$((t+5))
    done
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$PG_CONTAINER" 2>/dev/null)" = "healthy" ] \
        || { docker logs --tail 40 "$PG_CONTAINER"; die "postgres never became healthy"; }
    ok "postgres healthy"

    if db_installed; then
        ok "schema already installed (tb_schema_settings exists) — skipping"
        say "  to start from scratch: LAB_TB_YES=1 ./lab_tb_up.sh reset"
        return 0
    fi

    # LOAD_DEMO=true is NOT optional here: it is what creates the
    # tenant@thingsboard.org / tenant account that every tools/tb_*.py logs in
    # with (fleet_common.py:36-37). A clean install leaves only sysadmin.
    say "  running INSTALL_TB=true LOAD_DEMO=true (2-6 min, one time)..."
    dc run --rm -e INSTALL_TB=true -e LOAD_DEMO=true thingsboard-ce \
        || die "install failed — inspect the output above, then 'reset' and retry"
    ok "schema + demo data installed"
    say "  tenant login: $TENANT_USER / $TENANT_PASS"
}

do_up() {
    require_env
    hdr "starting the stack"
    dc up -d || die "compose up failed"
    docker ps --filter "name=tb-lab" --format 'table {{.Names}}\t{{.Status}}'
    wait_ready 420 || return 1
    say ""
    do_verify
}

do_down() {
    require_env
    hdr "stopping the stack (database preserved)"
    dc down
    ok "stopped. Data volume '$PG_VOLUME' kept."
}

do_reset() {
    require_env
    if [ "${LAB_TB_YES:-0}" != "1" ]; then
        die "reset DELETES the TB database (volume $PG_VOLUME). Re-run with LAB_TB_YES=1 to confirm."
    fi
    hdr "resetting: down + volume removal"
    dc down -v
    docker volume rm -f "$PG_VOLUME" >/dev/null 2>&1 || true
    ok "wiped. Next: ./lab_tb_up.sh install && ./lab_tb_up.sh up"
}

do_restart() {
    require_env
    # Leshan loads LwM2M object models into LwM2mModelProvider AT STARTUP, so a
    # restart is mandatory after every /api/resource upload
    # (tools/tb_edge_upload_models.py:365-366). Restarting TB only keeps the DB warm.
    hdr "restarting $TB_CONTAINER (required after uploading LwM2M models)"
    docker restart "$TB_CONTAINER" >/dev/null || die "restart failed"
    wait_ready 300
}

do_logs() {
    require_env
    if [ "${LAB_TB_FOLLOW:-0}" = "1" ]; then
        dc logs -f --tail "${LAB_TB_TAIL:-200}" thingsboard-ce
    else
        dc logs --tail "${LAB_TB_TAIL:-200}" thingsboard-ce
    fi
}

do_status() {
    require_env
    hdr "containers"
    docker ps -a --filter "name=tb-lab" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'

    hdr "listening sockets"
    if have ss; then
        ss -lunp 2>/dev/null | grep -E ":(5683|5684|5687|5688|5690)\b" || say "  (no LwM2M/CoAP UDP sockets)"
        ss -ltnp 2>/dev/null | grep -E ":($HTTP_PORT|$PG_PORT|1883)\b"  || say "  (no TB TCP sockets)"
    else
        warn "install iproute2 for socket visibility"
    fi

    hdr "wpan0"
    wpan0_addrs | sed 's/^/  /' || true
}

do_verify() {
    require_env
    local rc=0
    hdr "ACCEPTANCE 1/5 — containers running"
    if tb_running; then ok "$TB_CONTAINER up ($(docker inspect -f '{{.State.StartedAt}}' $TB_CONTAINER))"; else bad "$TB_CONTAINER not running"; rc=1; fi

    hdr "ACCEPTANCE 2/5 — LwM2M bound on UDP $LWM2M_PORT, IPv6-capable"
    local out; out="$(port_holders udp "$LWM2M_PORT")"
    if [ "$out" = "__NOSS__" ]; then
        warn "no 'ss' — falling back to the container log check only"
    elif [ -z "$out" ]; then
        bad "nothing is listening on udp/$LWM2M_PORT. Grep the log for BindException:"
        say "         docker logs $TB_CONTAINER 2>&1 | grep -i bindexception"
        rc=1
    else
        printf '%s\n' "$out" | sed 's/^/         /'
        # '*:5683' or '[::]:5683' = dual-stack wildcard (good).
        # '0.0.0.0:5683' = IPv4 ONLY -> the Thread mesh can never reach it.
        if printf '%s' "$out" | grep -qE '(\*|\[::\]):'"$LWM2M_PORT"; then
            ok "wildcard/IPv6 bind — reachable over the OMR prefix"
        elif printf '%s' "$out" | grep -q "0.0.0.0:$LWM2M_PORT"; then
            bad "IPv4-ONLY bind. Set LWM2M_BIND_ADDRESS=:: in docker-compose.yml and restart."
            rc=1
        else
            warn "unexpected bind address — confirm it covers $(omr_addr 2>/dev/null || echo 'the OMR address')"
        fi
        printf '%s' "$out" | grep -q 'java' || warn "the socket is not owned by java — is something else squatting $LWM2M_PORT?"
    fi

    hdr "ACCEPTANCE 3/5 — Leshan endpoint line in the log"
    local eplines
    eplines="$(docker logs "$TB_CONTAINER" 2>&1 | grep -iE "Started endpoint at coap://.*:$LWM2M_PORT|LwM2M .*transport started" | tail -3)"
    if [ -n "$eplines" ]; then
        printf '%s\n' "$eplines" | sed 's/^/         /'
        ok "endpoint logged"
    else
        bad "no 'Started endpoint at coap://...:$LWM2M_PORT'. Most likely cause: the CoAP transport grabbed the port first."
        docker logs "$TB_CONTAINER" 2>&1 | grep -i "bindexception" | tail -3 | sed 's/^/         /'
        rc=1
    fi

    hdr "ACCEPTANCE 4/5 — REST login as the tenant the tooling expects"
    if have curl; then
        local body; body="$(curl -s -m 10 -X POST "http://127.0.0.1:$HTTP_PORT/api/auth/login" \
            -H 'Content-Type: application/json' \
            -d "{\"username\":\"$TENANT_USER\",\"password\":\"$TENANT_PASS\"}" 2>/dev/null)"
        if printf '%s' "$body" | grep -q '"token"'; then
            ok "$TENANT_USER can log in — tools/tb_*.py will authenticate"
        else
            bad "login failed. Did 'install' run with LOAD_DEMO=true? Response: $(printf '%s' "$body" | head -c 200)"
            rc=1
        fi
    else
        warn "curl missing — check the UI manually at http://localhost:$HTTP_PORT"
    fi

    hdr "ACCEPTANCE 5/5 — mesh-side reachability"
    local omr; omr="$(omr_addr)"
    if [ -n "$omr" ]; then
        ok "serve address for the node: coap://[$omr]:$LWM2M_PORT"
        say "         publish that via SRP, then './lab_tb_up.sh srpinfo'"
    else
        bad "no OMR address on wpan0 — the OTBR is down; the node cannot reach TB regardless of TB's health."
        rc=1
    fi

    say ""
    if [ "$rc" -eq 0 ]; then
        say "BENCH TB IS SERVING. Next: upload models -> restart -> profile -> device -> SRP publish -> power-cycle the node."
    else
        say "BENCH TB NOT READY — see [FAIL] above."
    fi
    return "$rc"
}

do_srpinfo() {
    local omr; omr="$(omr_addr)"
    [ -n "$omr" ] || omr="<wpan0 OMR address — 'ip -6 addr show wpan0'>"
    hdr "what the node's DNS-SD query must resolve"
    cat <<EOF
  The firmware pins these three strings (src/lwm2m_discover.c:24-26) — they are a
  protocol contract, not config:

    instance : ThingsBoard-Edge
    service  : _lwm2m._udp.default.service.arpa.
    host     : thingsboard-edge.default.service.arpa.

  Strategy 1 (otDnsClientResolveService) takes address AND port from the SRV
  record. Strategy 2 (otDnsClientResolveAddress) resolves the host only and
  HARDCODES port 5683 — so publish 5683 and the two strategies agree.

  Required SRP records:
    host    thingsboard-edge  ->  $omr
    service ThingsBoard-Edge._lwm2m._udp  port $LWM2M_PORT

  Confirm on the OTBR (this is where "the node can't find TB" is proven or ruled out):
    wsl -d Ubuntu-24.04 -u root -- ot-ctl srp server host
    wsl -d Ubuntu-24.04 -u root -- ot-ctl srp server service

  Node-side sanity check that needs NO server at all:
    python tools/diag_get.py --local --addr <node OMR address>
EOF
}

# ── Dispatch ───────────────────────────────────────────────────────────────
ACTION="${1:-status}"
case "$ACTION" in
    preflight) do_preflight ;;
    pull)      do_pull ;;
    install)   do_install ;;
    up)        do_up ;;
    down)      do_down ;;
    reset)     do_reset ;;
    restart)   do_restart ;;
    logs)      do_logs ;;
    status)    do_status ;;
    verify)    do_verify ;;
    srpinfo)   do_srpinfo ;;
    bootstrap) # the whole first-run sequence
        do_preflight && do_pull && do_install && do_up ;;
    *)
        say "unknown action '$ACTION'"
        say "actions: preflight | pull | install | up | verify | status | logs | restart | down | reset | srpinfo | bootstrap"
        exit 2 ;;
esac
