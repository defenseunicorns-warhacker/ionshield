#!/usr/bin/env bash
#
# Prepare an Earth Studio working folder for a scenario:
#   - downloads scenario.kmz, keyframes.csv via the cache-busted catalog URLs
#   - writes recipe.json from /api/v3/scenarios/{id}/recipe
#   - drops a README.txt pointing at docs/earth-studio-workflow.md
#
# Usage:
#   ./scripts/prepare_scenario.sh <scenario_id> [base_url] [out_dir]
#
# Env:
#   IONSHIELD_API_KEY=...   if the deployment requires X-API-Key

set -euo pipefail

SID="${1:?scenario_id required, e.g. may-2024-g5}"
BASE="${2:-https://ionshield-demo-mvp.onrender.com}"
OUT="${3:-./build/$SID}"

if [[ -n "${IONSHIELD_API_KEY:-}" ]]; then
    AUTH=(-H "X-API-Key: ${IONSHIELD_API_KEY}")
else
    AUTH=()
fi

mkdir -p "$OUT"

echo "Preparing $SID under $OUT (host: $BASE)"

# 1. Pull the catalog and find the scenario's URLs (cache-busted).
catalog=$(curl -fsS "${AUTH[@]}" "$BASE/api/v3/scenarios")
sc=$(printf '%s' "$catalog" | python3 -c '
import json, sys, urllib.parse
sid = "'"$SID"'"
host = "'"$BASE"'"
catalog = json.load(sys.stdin)
match = next((s for s in catalog["scenarios"] if s["id"] == sid), None)
if match is None:
    sys.exit(f"unknown scenario: {sid}")
pc = match.get("precomputed") or {}
if not pc:
    sys.exit(f"scenario {sid} has no precomputed artifacts (live-only?)")
print(host + pc.get("kmz_url", ""))
print(host + pc.get("keyframes_url", ""))
')
kmz_url=$(printf '%s\n' "$sc" | sed -n 1p)
csv_url=$(printf '%s\n' "$sc" | sed -n 2p)

curl -fsSL "${AUTH[@]}" "$kmz_url" -o "$OUT/scenario.kmz"
curl -fsSL "${AUTH[@]}" "$csv_url" -o "$OUT/keyframes.csv"

# 2. Recipe from the per-scenario endpoint.
curl -fsS "${AUTH[@]}" "$BASE/api/v3/scenarios/$SID/recipe" \
    | python3 -m json.tool > "$OUT/recipe.json"

# 3. README pointing at the operator runbook.
cat > "$OUT/README.txt" << EOF
IonShield Earth Studio prep package — $(date -u +%Y-%m-%dT%H:%M:%SZ)
Scenario: $SID
Host: $BASE

Files:
  scenario.kmz       Layered KML/KMZ — drag into Earth Studio (Project → Import KML)
  keyframes.csv      Track-import CSV (Tracks panel → Add Track → Import CSV)
  recipe.json        Suggested camera path + render settings (duration, fps)

Follow the runbook: docs/earth-studio-workflow.md
EOF

echo "Done."
echo "  ${OUT}/scenario.kmz       $(wc -c < "$OUT/scenario.kmz") bytes"
echo "  ${OUT}/keyframes.csv      $(wc -c < "$OUT/keyframes.csv") bytes"
echo "  ${OUT}/recipe.json        $(wc -c < "$OUT/recipe.json") bytes"
echo
echo "Next: open Earth Studio and follow steps 2–7 of docs/earth-studio-workflow.md"
