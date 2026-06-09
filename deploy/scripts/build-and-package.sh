#!/usr/bin/env bash
# Build the hardened IonShield image and create the Zarf package.
# Run from the repo root:  ./deploy/scripts/build-and-package.sh [tag]
set -euo pipefail

TAG="${1:-0.1.0}"
IMAGE="ghcr.io/ionshield/ionshield:${TAG}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

echo "── 1/3 Building hardened image: ${IMAGE}"
docker build -f "${ROOT}/deploy/docker/Dockerfile.hardened" -t "${IMAGE}" "${ROOT}"

echo "── 2/3 Creating Zarf package (embeds the image — no registry needed on target)"
cd "${ROOT}/deploy/zarf"
zarf package create . --confirm

echo "── 3/3 Done. Artifacts:"
ls -lh "${ROOT}"/deploy/zarf/zarf-package-ionshield-*.tar.zst

cat <<'EOF'

Next steps:
  • Existing UDS/K8s cluster:   zarf package deploy deploy/zarf/zarf-package-ionshield-*.tar.zst --confirm
  • Air-gapped deploy:          add --set OFFLINE_MODE=true
  • Full local demo (k3d+core): cd deploy/uds && uds create . --confirm && uds deploy uds-bundle-ionshield-demo-*.tar.zst --confirm
EOF
