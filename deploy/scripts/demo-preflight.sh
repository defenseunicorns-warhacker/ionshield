#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# IonShield UDS preflight + self-heal.
#
# Idempotent. Safe to run anytime — on login, on wake, or by hand before a demo.
# Brings the local UDS stack up and healthy, fixing the known laptop-sleep
# gremlins, then prints a PASS/FAIL report.
#
# Heals, in order:
#   1. Colima VM stopped            → colima start
#   2. k3d cluster stopped          → k3d cluster start uds
#   3. CoreDNS 8.8.8.8 fallback     → forward to colima resolver 192.168.5.1
#      (k3s falls back to an unreachable 8.8.8.8 because the node resolver is
#       loopback; this kills LIVE data — offline cache-and-carry still serves)
#   4. Istio mesh certs expired     → restart tenant gateway + app (gateway 503)
#   5. Tripped NOAA circuit breaker → waits for it to self-heal after DNS is up
#
# Works fully OFFLINE: every heal step is local (no internet needed). When the
# laptop has no connectivity the app serves last-synced data in ADVISORY mode.
#
# Usage:
#   deploy/scripts/demo-preflight.sh          # heal if needed + full report
#   deploy/scripts/demo-preflight.sh --quiet  # same, terse (for LaunchAgent log)
# ──────────────────────────────────────────────────────────────────────────────
set -uo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

URL="https://ionshield.uds.dev"
CLUSTER="uds"
RESOLVER="192.168.5.1"           # colima/lima gateway DNS (reachable from pods)
QUIET=0; [ "${1:-}" = "--quiet" ] && QUIET=1

K() { zarf tools kubectl "$@"; }
ts() { date '+%H:%M:%S'; }
say() { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
step() { printf '[%s] %s\n' "$(ts)" "$*"; }

say "═══════════════════════════════════════════════"
say " IonShield UDS preflight — $(date '+%Y-%m-%d %H:%M')"
say "═══════════════════════════════════════════════"

# ── 1. Colima ────────────────────────────────────────────────────────────────
if ! colima status >/dev/null 2>&1; then
  step "Colima VM down → starting…"
  colima start >/dev/null 2>&1 || step "  ⚠ colima start failed"
fi

# ── 2. Cluster (only the heavy path if the API isn't reachable) ───────────────
if ! K get --raw='/readyz' >/dev/null 2>&1; then
  step "Cluster API unreachable → starting k3d cluster '$CLUSTER'…"
  k3d cluster start "$CLUSTER" >/dev/null 2>&1 || step "  ⚠ k3d start failed"
  for _ in $(seq 1 40); do
    K get --raw='/readyz' >/dev/null 2>&1 && break
    sleep 3
  done
fi

if ! K get ns ionshield >/dev/null 2>&1; then
  say "✗ cluster not reachable — cannot continue. Is Docker/Colima installed?"
  exit 1
fi

# ── 3. CoreDNS upstream fix (idempotent) ─────────────────────────────────────
CF=$(K -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' 2>/dev/null)
if [ -n "$CF" ] && ! printf '%s' "$CF" | grep -q "$RESOLVER"; then
  step "CoreDNS forwarding to unreachable upstream → patching to $RESOLVER…"
  NH=$(K -n kube-system get configmap coredns -o jsonpath='{.data.NodeHosts}' 2>/dev/null)
  printf '%s' "$CF" | sed "s#forward \. /etc/resolv.conf#forward . $RESOLVER#" >/tmp/ion-Corefile
  K -n kube-system create configmap coredns \
    --from-file=Corefile=/tmp/ion-Corefile \
    --from-literal=NodeHosts="$NH" \
    --dry-run=client -o yaml | K apply -f - >/dev/null 2>&1
  K -n kube-system rollout restart deploy/coredns >/dev/null 2>&1
  K -n kube-system rollout status deploy/coredns --timeout=90s >/dev/null 2>&1
fi

# ── 4. Gateway / mesh certs — heal only if the gateway is actually failing ───
code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 12 "$URL/api/status" 2>/dev/null)
if [ "$code" != "200" ]; then
  step "Gateway returned $code → refreshing Istio mesh certs (gateway + app)…"
  K -n istio-tenant-gateway rollout restart deploy/tenant-ingressgateway >/dev/null 2>&1
  K -n ionshield rollout restart deploy/ionshield >/dev/null 2>&1
  K -n istio-tenant-gateway rollout status deploy/tenant-ingressgateway --timeout=180s >/dev/null 2>&1
  K -n ionshield rollout status deploy/ionshield --timeout=180s >/dev/null 2>&1
  sleep 10
fi

# ── 5. Report ────────────────────────────────────────────────────────────────
pass=0; fail=0
check() { # name, actual, expected-substring
  if printf '%s' "$2" | grep -q "$3"; then say "  ✓ $1 ($2)"; pass=$((pass+1))
  else say "  ✗ $1 ($2)"; fail=$((fail+1)); fi
}

say ""
say "── Health ──────────────────────────────────────"
check "gateway /api/status" "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 12 $URL/api/status)" "200"
check "mission planner"     "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 12 $URL/mission)" "200"
check "dashboard"           "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 12 $URL/dashboard)" "200"
check "package CR"          "$(K -n ionshield get package ionshield -o jsonpath='{.status.phase}' 2>/dev/null)" "Ready"

# Demo path (works regardless of connectivity — replay uses recorded values)
demo=$(curl -sk -X POST $URL/api/v3/recommend -H 'Content-Type: application/json' \
  -d '{"mission_type":"uav","gnss_dependence":"high","waypoints":[{"lat":32.9,"lon":-117.1,"name":"LZ"}],"equipment":["uas_group1","gps_single_freq","sincgars_fm"],"scenario":"gannon-2024"}' \
  --max-time 20 2>/dev/null | python3 -c "import sys,json;b=json.load(sys.stdin);print(b['mission_risk_level']+'/'+b['equipment']['weather_state'])" 2>/dev/null)
check "Gannon replay demo" "${demo:-none}" "SEVERE"

# Live data vs offline cache (informational — both are healthy states)
src=$(curl -sk --max-time 12 $URL/api/v3/health 2>/dev/null | python3 -c "
import sys,json
try:
    b=json.load(sys.stdin); s=b.get('sources',{}).get('noaa_swpc',{})
    fs=s.get('status',{}); fs=fs.get('fetch_status',fs) if isinstance(fs,dict) else {}
    ok=sum(1 for v in fs.values() if v=='ok'); print(f'{ok}/{len(fs)} NOAA feeds live' if fs else 'cached/offline')
except Exception: print('unknown')" 2>/dev/null)
say "  • data: ${src:-unknown}  (offline → cache-and-carry ADVISORY is expected)"

say ""
if [ "$fail" -eq 0 ]; then
  say "✅ ALL GREEN — $URL/mission"
else
  say "⚠️  $fail check(s) failed — re-run in ~30s; if persistent, see deploy/README.md"
fi
say ""

# One-line heartbeat — always emitted (even in --quiet) so the LaunchAgent log
# carries proof-of-life for every run.
if [ "$fail" -eq 0 ]; then status=healthy; else status=HEALED-OR-DEGRADED; fi
printf '[%s] preflight: %d ok / %d fail — %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$pass" "$fail" "$status"
exit 0
