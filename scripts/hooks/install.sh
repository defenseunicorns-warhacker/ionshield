#!/usr/bin/env bash
# Install the IonShield git hooks into .git/hooks (one time per clone).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(git -C "$HERE" rev-parse --show-toplevel)"
install -m 0755 "$HERE/pre-commit" "$ROOT/.git/hooks/pre-commit"
echo "Installed pre-commit hook → $ROOT/.git/hooks/pre-commit"
echo "  (auto-runs ruff format + ruff check on staged .py before each commit)"
