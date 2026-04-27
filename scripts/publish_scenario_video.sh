#!/usr/bin/env bash
#
# Register a rendered Earth Studio mp4 with an IonShield scenario.
# Writes a per-scenario video.json sidecar; the catalog endpoint merges
# it into the scenario's video_url on every read so the Simulation page
# auto-picks it up. Source-controlled scenarios.json is never mutated.
#
# Usage:
#   ./scripts/publish_scenario_video.sh <id> <video_url> [--duration N] [--notes "text"]
#
# Examples:
#   ./scripts/publish_scenario_video.sh may-2024-g5 \
#       https://cdn.example.com/may-2024-g5.mp4 --duration 30
#
#   IONSHIELD_API_KEY=xxx ./scripts/publish_scenario_video.sh halloween-2003 \
#       https://cdn.example.com/halloween-2003.mp4 --duration 45 \
#       --notes "1080p 30fps render via Earth Studio"

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <scenario_id> <video_url> [--duration N] [--notes 'text'] [--host URL]" >&2
    exit 64
fi

SID="$1"; URL="$2"; shift 2
DURATION=""; NOTES=""; HOST="${IONSHIELD_HOST:-https://ionshield-demo-mvp.onrender.com}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --duration) DURATION="$2"; shift 2 ;;
        --notes)    NOTES="$2";    shift 2 ;;
        --host)     HOST="$2";     shift 2 ;;
        *) echo "unknown flag: $1" >&2; exit 64 ;;
    esac
done

if [[ -n "${IONSHIELD_API_KEY:-}" ]]; then
    AUTH=(-H "X-API-Key: ${IONSHIELD_API_KEY}")
else
    AUTH=()
fi

body=$(python3 -c "
import json, sys
out = {'video_url': '$URL'}
if '$DURATION': out['duration_seconds'] = float('$DURATION')
if '''$NOTES''': out['notes'] = '''$NOTES'''
print(json.dumps(out))
")

echo "Registering video for scenario '$SID' on $HOST"
echo "  body: $body"

response=$(curl -fsS -X POST \
    "$HOST/api/v3/scenarios/$SID/video" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d "$body")
echo "Response:"
echo "$response" | python3 -m json.tool

echo
echo "Verify at: $HOST/simulation"
echo "(reload the page; the scenario card now shows the embedded mp4)"
