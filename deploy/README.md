# Deploying IonShield on UDS (Defense Unicorns)

This directory contains everything needed to take IonShield from "runs on
Render" to "deploys on UDS / Kubernetes / air-gapped enclaves."

Written for a founder, not a platform engineer. Follow top to bottom.

---

## What's in here

| Path | What it is |
|---|---|
| `docker/Dockerfile.hardened` | Container build with swappable base images (Iron Bank-ready) |
| `chart/` | Helm chart — Deployment, Service, PVC, ConfigMap, **UDS Package CR** |
| `k8s/ionshield.yaml` | Plain Kubernetes manifests (no Helm needed) for non-UDS clusters |
| `zarf/zarf.yaml` | Zarf package — bundles image + chart into one air-gap-portable tarball |
| `uds/uds-bundle.yaml` | UDS bundle — k3d + UDS Core + IonShield, the "demo in a box" |
| `scripts/build-and-package.sh` | One command: build image → create Zarf package |

## The mental model (30 seconds)

- **Docker image** — IonShield frozen into a runnable box.
- **Helm chart** — instructions for running that box on Kubernetes.
- **Zarf package** — image + chart sealed into ONE tarball you can carry on a
  hard drive into a disconnected facility. This is Defense Unicorns' core tech.
- **UDS Core** — Defense Unicorns' secure platform layer (Istio service mesh,
  Keycloak SSO, Pepr policy engine) that runs on the cluster.
- **UDS Package CR** — a small YAML our chart includes that tells UDS Core
  "expose IonShield at `https://ionshield.<domain>`, and allow exactly this
  network traffic, deny everything else."
- **UDS bundle** — UDS Core + IonShield together, one `uds deploy` command.

---

## 0. Install the tools (Mac)

```bash
brew install helm zarf defenseunicorns/tap/uds
# Docker Desktop: https://docker.com/products/docker-desktop (open it once after install)
```

## 1. Build the hardened image

```bash
# from the repo root
docker build -f deploy/docker/Dockerfile.hardened -t ghcr.io/ionshield/ionshield:0.1.0 .
```

For a real defense deployment, swap the runtime base for an Iron Bank image
(requires a free Platform One account at registry1.dso.mil):

```bash
docker build -f deploy/docker/Dockerfile.hardened \
  --build-arg RUNTIME_BASE=registry1.dso.mil/ironbank/opensource/python:v3.12 \
  -t ghcr.io/ionshield/ionshield:0.1.0 .
```

## 2. Create the Zarf package

```bash
cd deploy/zarf
zarf package create . --confirm
# → zarf-package-ionshield-arm64-0.1.0.tar.zst  (or amd64)
```

That tarball **contains the container image**. It needs no internet and no
registry on the target side. This is the artifact you'd hand a government
platform team.

> Building for a govcloud/x86 target from an Apple Silicon Mac? Build the
> image with `--platform linux/amd64` and add `--architecture amd64` to
> `zarf package create`.

Or do steps 1+2 in one shot: `./deploy/scripts/build-and-package.sh`

## 3. Demo it locally — full UDS stack on your laptop

```bash
cd deploy/uds
uds create . --confirm
uds deploy uds-bundle-ionshield-demo-*.tar.zst --confirm
```

This stands up a local k3d cluster, installs UDS Core (slim), deploys
IonShield, and exposes it through the Istio tenant gateway at:

**https://ionshield.uds.dev** (resolves to localhost automatically)

This is the WarHacker money shot: *"IonShield running on your platform,
packaged with your tooling, deployed with one command."*

## 4. Deploy into an existing UDS cluster (what a customer would do)

```bash
zarf package deploy zarf-package-ionshield-*.tar.zst --confirm
```

Air-gapped (no NOAA egress — serves archived data + storm replay):

```bash
zarf package deploy zarf-package-ionshield-*.tar.zst --set OFFLINE_MODE=true --confirm
```

## 5. Plain Kubernetes (no UDS) — fallback

```bash
kubectl apply -f deploy/k8s/ionshield.yaml
kubectl -n ionshield port-forward svc/ionshield 8000:8000
open http://localhost:8000
```

---

## Connected vs. disconnected operation

| Component | Connected | Disconnected (OFFLINE_MODE=true) |
|---|---|---|
| NOAA SWPC live feeds (9 endpoints, 5-min cadence) | live | **disabled** — last archived snapshot served, staleness reported honestly in decision confidence |
| NASA CDAWeb HAPI (historical backfill) | live | disabled |
| Physics models (GPS error, HF, PCA, SATCOM, radar) | local | **local — fully functional** |
| Decision engine + Mission Planner | local | **local — fully functional** |
| ML (Kp forecaster, event classifier, retraining) | local | **local** (trains on archived data) |
| Storm replay / simulation (May 2024 Gannon storm etc.) | local | **local — this is the air-gap demo** |
| ATAK CoT (pull endpoint + TAK server push) | local/enclave | **local/enclave — works air-gapped** |
| KML/KMZ/GeoJSON/Parquet exports | local | **local** |
| Cesium 3D globe | OSM tiles + bundled imagery | **bundled NaturalEarthII imagery** (low-res but fully offline) |
| SQLite archive | local | local |
| Foundry sync, SMTP contact form | optional egress | disabled (config off) |

A hybrid pattern for real enclaves: run one connected relay instance that
mirrors SWPC feeds across a one-way diode, and point enclave instances at it
via `SWPC_BASE_URL`. The setting exists for exactly this.

## Security posture

- Container: non-root (UID 1001), no capabilities, no privilege escalation,
  swappable Iron Bank base.
- Pod: `runAsNonRoot`, seccomp default via UDS Core, single replica + PVC.
- Network: UDS Package CR yields **default-deny** NetworkPolicies; the only
  egress allowed is 443 to the feed sources (and that disappears under
  `OFFLINE_MODE=true`). Ingress only via the Istio tenant gateway.
- App: per-tenant Bearer auth, audit log on every /api/v3 request, SHA-256
  input provenance, rate limiting, security headers.
- SSO: flip `udsPackage.sso.enabled=true` to put UDS Keycloak in front.

## Known gaps / honest to-do list

1. **Image not yet in a registry** — `ghcr.io/ionshield/ionshield` is a
   placeholder. Push there or rename in `chart/values.yaml` + `zarf/zarf.yaml`.
2. **UDS Core version pin** — `uds-bundle.yaml` pins `k3d-core-slim-dev:0.47.0`;
   check the latest release before the event.
3. **Read-only root filesystem** — the app writes ML artifacts and scenario
   assets inside the container; moving those to mounted volumes would allow
   `readOnlyRootFilesystem: true`. Documented, not yet done.
4. **Postgres option** — SQLite+PVC is fine single-replica; an enclave wanting
   HA needs `DATABASE_URL` pointed at a Postgres (code already supports it).
5. **SBOM/scanning** — Zarf generates SBOMs at package-create time
   (`zarf package inspect` shows them); CI gate not yet wired.
