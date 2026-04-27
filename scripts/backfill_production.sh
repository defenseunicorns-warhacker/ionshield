#!/usr/bin/env bash
#
# Trigger the historical-storm backfill on a deployed IonShield instance.
#
# Usage:
#   ./scripts/backfill_production.sh                       # default base URL
#   ./scripts/backfill_production.sh https://my.host       # custom host
#   IONSHIELD_API_KEY=xxx ./scripts/backfill_production.sh # if API key is set
#
# Idempotent on the server side — re-running is a no-op for already-backfilled
# windows. Hits the public POST /api/v3/scenarios/backfill endpoint.

set -euo pipefail

BASE_URL="${1:-https://ionshield-demo-mvp.onrender.com}"
PROFILES=(may-2024-g5 halloween-2003 st-patrick-2015)

if [[ -n "${IONSHIELD_API_KEY:-}" ]]; then
    AUTH=(-H "X-API-Key: ${IONSHIELD_API_KEY}")
else
    AUTH=()
fi

echo "Backfilling historical storms on ${BASE_URL}..."
echo

for p in "${PROFILES[@]}"; do
    printf '  %-22s ' "$p"
    response=$(curl -fsS -X POST "${BASE_URL}/api/v3/scenarios/backfill?profile_id=${p}" \
        "${AUTH[@]}" -H 'Content-Type: application/json' -d '{}' || echo '{"error":"request_failed"}')
    echo "${response}" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception as exc:
    print(f'parse error: {exc}'); sys.exit(0)
results = d.get('results', [])
if not results:
    print(d); sys.exit(0)
r = results[0]
print(
    f\"inserted={r.get('inserted', 0):>4}  \"
    f\"peak_kp={r.get('peak_kp', '-'):>4}  \"
    f\"min_bz={r.get('min_bz_nt', '-'):>6}  \"
    f\"peak_wind={r.get('peak_wind_km_s', '-')}  \"
    f\"reason={r.get('reason', '-')}\"
)
"
done

echo
echo "Done. Verify by hitting:"
echo "  ${BASE_URL}/simulation"
echo "and clicking one of the storm cards."
