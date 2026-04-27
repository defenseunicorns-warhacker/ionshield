#!/usr/bin/env bash
#
# Trigger scenario precomputation on a deployed IonShield instance.
# Run this AFTER scripts/backfill_production.sh so the historical-storm
# rows exist in noaa_snapshots.
#
# Usage:
#   ./scripts/precompute_scenarios.sh                       # default base URL
#   ./scripts/precompute_scenarios.sh https://my.host       # custom host
#   IONSHIELD_API_KEY=xxx ./scripts/precompute_scenarios.sh # if API key is set

set -euo pipefail

BASE_URL="${1:-https://ionshield-demo-mvp.onrender.com}"

if [[ -n "${IONSHIELD_API_KEY:-}" ]]; then
    AUTH=(-H "X-API-Key: ${IONSHIELD_API_KEY}")
else
    AUTH=()
fi

echo "Precomputing scenarios on ${BASE_URL}..."
response=$(curl -fsS -X POST "${BASE_URL}/api/v3/scenarios/precompute" \
    "${AUTH[@]}" -H 'Content-Type: application/json' -d '{}' \
    || echo '{"error":"request_failed"}')

echo "${response}" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception as exc:
    print(f'parse error: {exc}'); sys.exit(0)
results = d.get('results', [])
for r in results:
    sid = r.get('scenario_id', '?')
    if r.get('written'):
        gj = r.get('geojson_bytes', 0) / 1024
        kmz = r.get('kmz_bytes', 0) / 1024
        csv = r.get('keyframes_bytes', 0) / 1024
        print(f'  {sid:22s}  features={r.get(\"n_features\", 0):>5}  '
              f'gj={gj:>7.1f}KB  kmz={kmz:>6.1f}KB  csv={csv:>6.1f}KB')
    else:
        print(f'  {sid:22s}  SKIPPED  reason={r.get(\"skipped_reason\", \"?\")}')
"

echo
echo "Verify by hitting:"
echo "  ${BASE_URL}/simulation"
echo "and clicking one of the scenario cards — Network tab should show"
echo "/static/scenarios/<id>/scenario.geojson rather than /api/v3/scenarios/export"
