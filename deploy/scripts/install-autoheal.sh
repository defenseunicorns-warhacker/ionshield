#!/usr/bin/env bash
# Install (or refresh) the IonShield UDS auto-heal LaunchAgent.
#
# Copies demo-preflight.sh to ~/.ionshield (a non-TCC-protected path launchd
# can execute — unlike the Desktop) and loads the LaunchAgent that runs it at
# login and every 5 minutes. Re-run this any time you change demo-preflight.sh.
#
#   ./deploy/scripts/install-autoheal.sh           # install / update
#   ./deploy/scripts/install-autoheal.sh --remove  # uninstall
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="$HOME/.ionshield"
DEST="$DEST_DIR/demo-preflight.sh"
AGENT="$HOME/Library/LaunchAgents/com.ionshield.preflight.plist"

if [ "${1:-}" = "--remove" ]; then
  launchctl unload "$AGENT" 2>/dev/null || true
  rm -f "$AGENT" "$DEST"
  echo "Removed auto-heal LaunchAgent and ~/.ionshield/demo-preflight.sh"
  exit 0
fi

mkdir -p "$DEST_DIR"
cp "$HERE/demo-preflight.sh" "$DEST"
chmod +x "$DEST"
cp "$HERE/com.ionshield.preflight.plist" "$AGENT"

launchctl unload "$AGENT" 2>/dev/null || true
launchctl load "$AGENT"

echo "Installed:"
echo "  script : $DEST"
echo "  agent  : $AGENT  (RunAtLoad + every 5 min)"
echo "  log    : /tmp/ionshield-preflight.log"
echo
echo "Runs automatically on login and wake. Force a run now:"
echo "  bash $DEST"
