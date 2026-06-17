# Deploying IonShield on UDS (Defense Unicorns)

IonShield is **PNT & communications mission assurance**: it translates real-time
space weather into go / modify / delay decisions for GPS-, communications-, and
autonomy-dependent missions. This directory takes it from "runs on Render" to
"deploys on UDS / Kubernetes / air-gapped enclaves."

Written for a founder, not a platform engineer. Follow top to bottom.

> **Current deployed image tag:** `0.1.8` (operational feeds: D-RAP, live GPS
> availability via CelesTrak GPS-ops, NASA DONKI, OVATION aurora, local WMM). Bump
> this on every backend or UI change — see *Updating a running deployment* below.
>
> **Optional env — `NASA_API_KEY`:** the DONKI event feed defaults to NASA's
> shared `DEMO_KEY`, which is heavily rate-limited (you will see `http_429` and
> the feed will fall back to last-good cache). For a real quota, get a free key
> at <https://api.nasa.gov> and set `NASA_API_KEY` in the deployment env. All
> other feeds (D-RAP, CelesTrak, OVATION, WMM) need no key; WMM is computed
> locally and works fully air-gapped.

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
| `scripts/demo-preflight.sh` | Idempotent up + self-heal + PASS/FAIL report (run before any demo) |
| `scripts/install-autoheal.sh` | Installs the LaunchAgent that runs preflight on login + every 5 min |

## Keeping it always-up (laptop sleep / reboot)

The local UDS stack runs inside Colima (a VM). When the laptop sleeps or
reboots, a few things break — and `demo-preflight.sh` heals all of them
idempotently:

1. **Colima VM stopped** → `colima start`
2. **k3d cluster stopped** → `k3d cluster start uds`
3. **CoreDNS dead upstream** → k3s falls back to an unreachable `8.8.8.8`
   because the node resolver is loopback; the script repoints it at Colima's
   reachable resolver `192.168.5.1`. (Only affects *live* data — offline,
   cache-and-carry keeps serving.)
4. **Istio mesh certs expired** (gateway 503 after ~24 h idle) → restart the
   tenant gateway + app pod
5. **Tripped NOAA circuit breaker** → self-heals once DNS is back

**Automatic:** `install-autoheal.sh` registers a macOS LaunchAgent that runs
the preflight at login and every 5 minutes (the interval catches wake-from-
sleep). When healthy it's a fast no-op; it only heals what's actually down.
A full cold start (Colima off → all green, 7/7 live feeds) takes ~60 s.

```bash
./deploy/scripts/install-autoheal.sh        # one-time setup
bash ~/.ionshield/demo-preflight.sh         # force a run / verify before a demo
tail -f /tmp/ionshield-preflight.log        # watch the heartbeat
./deploy/scripts/install-autoheal.sh --remove   # uninstall
```

Honest limits: a laptop dev cluster is not cloud HA. On wake or after a
break it can take up to the 5-minute interval to self-heal — so **before a
live demo, run `demo-preflight.sh` by hand** for an instant green. The agent
only runs while you're logged in.

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

Already done on this machine (Jun 2026): helm via brew; zarf + uds as direct
binaries in `~/.local/bin` (on PATH via .zshrc); Docker via Colima.

```bash
brew install helm k3d colima docker docker-buildx
mkdir -p ~/.docker/cli-plugins && ln -sf /opt/homebrew/opt/docker-buildx/bin/docker-buildx ~/.docker/cli-plugins/docker-buildx
colima start --cpu 4 --memory 8 --disk 40

# zarf + uds-cli — direct binaries (brew tap builds from source and needs
# current Xcode CLT; the release binaries don't):
mkdir -p ~/.local/bin
curl -sL -o ~/.local/bin/zarf  https://github.com/zarf-dev/zarf/releases/download/v0.77.0/zarf_v0.77.0_Darwin_arm64
curl -sL -o ~/.local/bin/uds   https://github.com/defenseunicorns/uds-cli/releases/download/v0.32.0/uds-cli_v0.32.0_Darwin_arm64
chmod +x ~/.local/bin/zarf ~/.local/bin/uds
export PATH="$HOME/.local/bin:$PATH"
```

> After a reboot: `colima start` brings Docker back; `k3d cluster list`
> shows the demo cluster.

## Updating a running deployment (after a code or UI change)

A redeploy with the **same** image tag will NOT roll the pod — UDS/Zarf rewrites
images to its internal registry, and the Deployment spec doesn't change, so the
old pod keeps running. Always **bump the tag** for any code/UI change:

```bash
# 1. Bump the tag in BOTH places (e.g. 0.1.3 → 0.1.4):
#      deploy/chart/values.yaml   image.tag
#      deploy/zarf/zarf.yaml      IMAGE_TAG constant + images[] entry
# 2. Rebuild, repackage, redeploy (remove+deploy guarantees a clean pull):
docker build -f deploy/docker/Dockerfile.hardened -t ghcr.io/ionshield/ionshield:<tag> .
cd deploy/zarf && zarf package create . --confirm
zarf package remove   zarf-package-ionshield-*.tar.zst --confirm
zarf package deploy   zarf-package-ionshield-*.tar.zst --confirm
# 3. Refresh the Istio mesh cert (gateway 503s after ~24h idle or a pod roll):
zarf tools kubectl -n istio-tenant-gateway rollout restart deploy/tenant-ingressgateway
# 4. Verify:
bash ~/.ionshield/demo-preflight.sh
```

## 1. Build the hardened image

```bash
# from the repo root
docker build -f deploy/docker/Dockerfile.hardened -t ghcr.io/ionshield/ionshield:0.1.3 .
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

## 3. Demo it locally — UDS stack on your laptop

UDS Core ships as its own bundle (bundles can't nest), so the demo is
two commands:

```bash
# 1. Local k3d cluster + UDS Core slim (Istio gateway + Pepr policy engine).
#    Check https://github.com/defenseunicorns/uds-core/releases for latest.
uds deploy k3d-core-slim-dev:1.6.0 --confirm

# 2. IonShield into it:
cd deploy/zarf
zarf package deploy zarf-package-ionshield-*.tar.zst --confirm
```

→ **https://ionshield.uds.dev** (resolves to localhost automatically)

This is the WarHacker money shot: *"IonShield running on your platform,
packaged with your tooling, deployed with one command."*

### 3b. Plain k3d fallback (no UDS Core — verified working on this machine)

If the UDS Core download misbehaves (large ghcr pulls can be flaky on some
networks), the Zarf air-gap story still demos perfectly on a bare cluster:

```bash
k3d cluster create uds          # skip if it exists
zarf init --confirm             # if the init pkg download fails, grab it from
                                # github.com/zarf-dev/zarf/releases and re-run
cd deploy/zarf
zarf package deploy zarf-package-ionshield-*.tar.zst \
  --set OFFLINE_MODE=true --set UDS_PACKAGE_ENABLED=false --confirm
zarf tools kubectl -n ionshield port-forward svc/ionshield 8800:8000
open http://localhost:8800/mission
```

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
