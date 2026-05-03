# IonShield — Product Overview, Architecture & Strategy

> A single document covering what IonShield is, how it works, what we've built,
> what's next, and the strategic plan for going from working demo to revenue.

**Last updated:** 2026-04-29
**Live demo:** https://ionshield-demo-mvp.onrender.com
**Repo:** https://github.com/jjwest3/IonShield_Demo_MVP

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [The product today](#2-the-product-today)
3. [Architecture](#3-architecture)
4. [ML — current and direction](#4-ml--current-and-direction)
5. [Industries served](#5-industries-served)
6. [Surfaces (what users see)](#6-surfaces-what-users-see)
7. [Deployment](#7-deployment)
8. [What was built in this session](#8-what-was-built-in-this-session)
9. [Strategy memo](#9-strategy-memo)
   - [Hosting strategy](#91-hosting-strategy)
   - [Government readiness — gap analysis](#92-government-readiness--gap-analysis)
   - [Feature roadmap (ROI-ranked)](#93-feature-roadmap-roi-ranked)
   - [Pricing & packaging](#94-pricing--packaging)
   - [Wedge-customer strategy](#95-wedge-customer-strategy)
   - [Funding path](#96-funding-path)
   - [30/60/90 day plan](#97-306090-day-plan)
   - [Open questions](#98-open-questions)
10. [Key links](#10-key-links)

---

## 1. Executive summary

**The problem.** GPS, HF radio, SATCOM, and RTK are degraded by space weather
(solar flares, geomagnetic storms, ionospheric scintillation) — sometimes
globally, more often over a specific region for a specific window. NOAA
publishes the raw drivers (Kp index, IMF Bz, X-ray flux, proton flux, TEC grid)
but doesn't tell an operator *what to do*. Most operators end up either ignoring
the risk or reading bulletins they can't act on.

**What IonShield does.** Translates that raw data into operator-readable
answers. Instead of "Kp = 7," you get:

- **GPS L1 error: +18 m at this AOI from 14:20–16:40 UTC**
- **HF blackout: ~42 minutes on the polar leg, switch to SATCOM-Ka**
- **Route verdict: Leg 2 ADVISORY, Leg 3 NO-GO until 22:00**

Same physics-based models everyone in the field uses (Klobuchar, CCIR-888,
Bailey PCA, Nakagami-m) — applied to your specific lat/lon, your specific
platform, your specific time window.

**One sentence.** IonShield is the translation layer between raw space weather
and operator decisions, for any team whose day breaks when GPS or comms
degrade.

**Real impact reference.** The May 2024 Gannon storm (G5, Kp 9) caused
~$500M in agricultural losses — RTK autosteer dropped during peak planting.
SpaceX lost 40 Starlinks in Feb 2022 to a single G2 storm. These events are
not hypothetical.

---

## 2. The product today

### What ships, live, right now

| Capability | Status | Surface |
|---|---|---|
| 5-min ingestion (NOAA SWPC + NASA OMNI + GFZ) | ✅ Live | API |
| 324-cell global risk grid | ✅ Live | Dashboard, API |
| GPS error / HF / SATCOM / radar models | ✅ Live | API, dashboard |
| 24-hour Kp forecaster (ML) | ✅ Live | `/api/v3/forecast/kp` |
| Event classifier (ML) | ✅ Live | Internal + audit |
| Champion/challenger A/B with auto-promote | ✅ Live | Internal |
| 3D Cesium dashboard with route + waypoint analysis | ✅ Live | `/dashboard` |
| Simulation mode (May 2024 G5, Halloween 2003, St. Patrick's 2015) | ✅ Live | `/simulation` |
| ATAK / WinTAK / iTAK network-link KML | ✅ Live | `/atak/network-link.kml` |
| ATAK DDIL pack (offline KMZ) | ✅ Live | `/atak/offline-pack.kmz` |
| CoT 2.0 feed | ✅ Live | `/overlay/ionshield.cot` |
| Foundry-compatible ontology + Workshop pack | ✅ Live | `/api/v3/foundry/pack` |
| Per-tenant Bearer-token auth + audit log | ✅ Live | `/api/v3/admin/keys` |
| Interactive API console | ✅ Live | `/api-console` |
| Integrations hub | ✅ Live | `/integrations` |
| ML strategy page | ✅ Live | `/ml` |

### Codebase scale (rough)

- ~50 Python modules in `app/`
- 12 marketing/product pages in `app/pages/`
- React + CesiumJS frontend in `frontend/`
- 470+ backend tests, 65 frontend tests
- Lint clean (ruff), CI green

---

## 3. Architecture

### Data flow

```
NOAA SWPC + NASA OMNI + GFZ Potsdam
            │
            ▼  (5-min cadence)
  ┌──────────────────────┐
  │   Ingestion layer    │  app/data/noaa.py + ustec.py
  │   Circuit breakers   │  app/data/circuit_breaker.py
  └──────────────────────┘
            │
            ▼
  ┌──────────────────────┐
  │   324-cell fusion    │  app/data/fusion.py
  │   geomag-lat scaled  │  app/models/ontology.py
  └──────────────────────┘
            │
            ▼
  ┌──────────────────────┐
  │   Physics models     │  app/models/risk.py
  │  Klobuchar/CCIR/PCA  │  app/models/impact.py
  └──────────────────────┘
            │
            ├───── Translated outputs ─────►  /api/v3/risk-map
            ├───── Decision engine    ─────►  /api/v2/route-decision
            ├───── ML classifier      ─────►  app/models/ml_classifier.py
            ├───── 24h Kp forecaster  ─────►  app/models/kp_forecaster.py
            └───── Foundry sync       ─────►  app/data/foundry_sync.py (Parquet)
```

### How it works under the hood

1. **Ingest** — pulls NOAA SWPC + NASA OMNI + GFZ Potsdam every 5 minutes
   (Kp, Bz, X-ray, proton, solar wind, F10.7, GloTEC TEC grid). Backfilled
   with historical archive data going back to the 1960s.
2. **Fuse** — merges feeds onto a 324-cell global grid (10° lat × 20° lon)
   with geomagnetic-latitude correction.
3. **Model** — runs Klobuchar+Mannucci for GPS error, CCIR-888 + Bailey PCA
   for HF, Nakagami-m for SATCOM, Skolnik for radar.
4. **Translate** — each region gets a typed verdict
   (NOMINAL / ELEVATED / DEGRADED / SEVERE) plus the operator-readable
   numbers.
5. **Serve** — REST API, KML/CoT for ATAK, Parquet for Foundry-style data
   platforms, GeoJSON for QGIS/Mapbox, KMZ for Google Earth.

### Key auth and audit primitives

- Per-tenant Bearer tokens minted via `POST /api/v3/admin/keys`
- Every `/api/v3/*` request logged in `api_audit_log` table with tenant id,
  status, IP, user-agent
- SHA-256 input hashes on every decision (deterministic replay)
- Champion/challenger A/B with auto-promotion in `app/models/auto_pilot.py`

---

## 4. ML — current and direction

### Live today

- **Event classifier** — multinomial logistic regression over 5 storm classes
  (BACKGROUND / GEOMAG_MAIN / SEP_EVENT / FLARE_M / FLARE_X). Pure-Python,
  JSON-readable weights. 7-feature input vector
  (kp, kp_max_3h, log10 X-ray flux, log10 X-ray max, log10 proton flux,
  wind speed, Bz).
- **24-hour Kp forecaster** — multi-horizon ridge regression trained on the
  NASA OMNI archive. Predicts Kp at +1, +3, +6, +12, +24 h with G-scale
  severity. Closed-form solver `(XᵀX + λI)⁻¹Xᵀy`, deterministic, milliseconds
  to train.
- **Champion / challenger A/B** — every new model registers as a shadow
  challenger, runs alongside the live champion on real traffic, auto-promotes
  when it wins by margin. Auto-pilot loop runs the cycle on a cooldown.

### Near-term (1–3 mo, committed direction)

- **Customer-tuned model versions** — each customer's deployment grows its
  own model trained on its own RTK / GPS C/N0 / HF SNR / mission outcomes.
  Same foundation, customer-specific advantage.
- **Per-region heads** — specialised forecasters per geomagnetic-latitude
  band (polar, mid-lat, equatorial anomaly).
- **Calibrated probabilities** — Platt / isotonic scaling so a "70% blackout"
  is empirically 70%.

### Long arc (6–12 mo)

- **Mission-history-informed recommendations** — accumulated outcomes tell
  the system which decisions worked under which conditions for which assets.
- **Storm-phase awareness** — system says "wait 90 min for recovery,"
  not just "monitor."
- **Autonomous suggestions on cleared workflows** — for routine,
  low-criticality decisions ("shift the harvest pass to morning"), the model
  can act with operator-defined authorisation. Defense and high-criticality
  always stays human-in-loop.

### The arc, in one sentence

Public data trains the baseline → fleet outcomes train *your* model → the
model becomes the operator (suggesting first, deciding next).

### Why this approach (not deep learning)

- **Auditable weights** — pure-Python regressors with JSON weights you can
  print to a screen. Compliance reviewers can read the model.
- **Shadow mode before promote** — no model ships to production users
  without proving itself on real traffic alongside the champion.
- **Physics as the floor** — when the ML disagrees with physics-grounded
  models, physics wins by default. The ML adds, doesn't replace.
- **Operator outcomes drive learning** — your team's outcomes are
  first-class training signal. The model gets better at *your* operator's
  job, not at a benchmark leaderboard.

---

## 5. Industries served

Defense isn't the only one — it's just the loudest.

| Industry | What they care about | Cost of a bad day |
|---|---|---|
| **Defense / ATAK / forward-deployed** | HF blackouts during ops, GPS-denied environments, fallback comms timing | Mission failure, lives |
| **UAV / BVLOS / drone** | RTK fix integrity for inspection / spray / mapping flights | $4–12k per aborted mission, BVLOS waiver risk |
| **Precision agriculture** | Autosteer + variable-rate applicators rely on RTK | $5–15/acre overlap + rework; ~$500M during May 2024 Gannon |
| **Aviation / polar dispatch** | Polar-route divert decisions, GNSS integrity windows | $50–80k per polar diversion |
| **Maritime / SAR** | Long-haul HF planning; SAR coordination depends on links | SAR delays measured in hours |
| **Surveying / construction** | A bad-fix day burns RTK crew + equipment | $2–6k per lost field day |
| **Autonomous mining / off-road** | Driverless haul trucks lose DGPS, drop to manual | 5–10% throughput loss / shift |
| **Satellite / SATCOM ops** | Link-margin scheduling around scintillation | Re-booked slot fees, delivery penalties |
| **Insurance / parametric** | Need attested data for crop insurance, aviation underwriting | Direct B2B revenue |
| **Power grid (FERC)** | GIC-induced transformer damage during severe storms | $13B (2003 Quebec blackout) |

---

## 6. Surfaces (what users see)

- **3D Live Dashboard** — CesiumJS globe, drop waypoints, get per-leg risk,
  Replay drawer for 30-day archive. `/dashboard`
- **Simulation** — replay May 2024 G5, Halloween 2003, St. Patrick's 2015
  hour-by-hour against your AOI. `/simulation`
- **API + interactive console** — paste Bearer token, hit any endpoint, get
  syntax-highlighted JSON. `/api-console`
- **Integrations hub** — every pathway (ATAK, API, GIS, Foundry, web overlay,
  alerts) in one place. `/integrations`
- **ATAK / WinTAK / iTAK** — one-click network-link KML, DDIL pack for offline
  ops, CoT 2.0 feed. `/atak`
- **Foundry-compatible** — ontology objects + Workshop layout + Parquet writes
  for customer-environment deployment. `/foundry`
- **ML page** — current capabilities + customer-tuned + autonomous-decision
  arc. `/ml`

---

## 7. Deployment

### Current

- **Public cloud demo** — Render (free tier currently). What's running on
  https://ionshield-demo-mvp.onrender.com.
- **CI/CD** — GitHub Actions → main → Render auto-deploy. Image also
  publishes to GHCR.
- **Database** — SQLite on Render persistent disk (when on paid tier).

### Available paths (planned)

- **AWS commercial** — when first paying customer requires SOC 2 or
  region/SLA commitments
- **AWS GovCloud** — IL2 → IL4 path for federal/defense customers
- **On-prem / air-gap** — pilot engagement; containerised stack runs against
  an on-prem NOAA mirror with no external calls
- **Customer-environment (Foundry-compatible)** — deploys inside customer
  perimeter; no formal Palantir partnership implied

---

## 8. What was built in this session

Chronological log of the major moves.

### Phase 1 — backend foundation
- Per-tenant Bearer-token auth (`/api/v3/admin/keys`, mint / list / revoke)
- Audit log table + middleware on every `/api/v3/*` request
- Full test coverage on auth flow

### Phase 2 — ML
- 24-hour Kp forecaster (ridge regression, multi-horizon)
- `/api/v3/forecast/kp` endpoint + admin retrain route
- Auto-bootstrap on startup if artifact missing

### Phase 3a — ATAK pack
- `/atak/network-link.kml` — auto-refreshing operator overlay
- `/atak/offline-pack.kmz` — DDIL fallback with 24h forecast
- `/atak` install guide
- Native plugin scaffolding directory (`atak-plugin/`)

### Phase 3b — Foundry pack
- `/api/v3/foundry/pack` — ontology + sample SQL + Workshop layout
- `/foundry` install guide
- Foundry sync writes Parquet (preview works without manual schema)
- SNAPSHOT-on-deploy migration to clean legacy JSONL

### Phase 3.5 — Integrations Hub + API Console
- `/integrations` central hub with live status strip
- `/api-console` interactive endpoint explorer with token persistence

### Product overhaul attempt (later reverted, then partially re-applied)
- Full design system in `marketing.css`
- Rewrote landing, features, integrations, simulation, use-cases, ml pages
- Restored simulation as a real product page (landing → run page split)
- Sandbox simulator for customer demos
- 24 problem/translate/decision rows on use-cases page
- Camera fixes on Globe.jsx (oblique aerial, no surface dive)
- Translation summary panel on dashboard
- HelpTip component replacing dead `?` buttons

### Final state (after revert + selective re-apply)
- Phases 1, 2, 3a, 3b, 3.5 — all retained
- Pages reverted to pre-overhaul layout
- Then surgical cherry-picks:
  - **Phase A** — `/ml` page restored (rewritten with stronger
    customer-feedback / autonomous-decision framing)
  - **Phase B** — Dashboard Simulation CTA shrunk to a normal-sized button
  - **Phase C** — Waypoint zoom fixed (oblique aerial, 600 km floor,
    no surface dive)
  - **Phase D attempted** — Overhaul landing tried, user didn't like, reverted

### Defaults that survived
- Per-tenant auth + audit log
- 24h Kp forecaster
- ATAK pack
- Foundry pack
- Integrations hub + API Console
- ML page (`/ml`)
- Dashboard normalised CTA
- Globe camera fix

---

## 9. Strategy memo

### 9.1 Hosting strategy

Three lanes, not one.

| Lane | Where | Serves | When we move |
|---|---|---|---|
| **Public demo** | Render (paid tier $7/mo) | Marketing site, evaluation, free tier, public CoT/KML | Now → forever |
| **Commercial production** | AWS us-east + AWS Marketplace | Paid commercial customers (ag, UAV, aviation, maritime) | When 1 paying customer >$5k/mo |
| **Federal / sovereign** | AWS GovCloud (IL2 → IL4) or Azure Gov | DoD, intel community, FAA, federal civilian | When SBIR award or LOI from a fed customer |

**Why three:** Render = 10× dev velocity for demo. AWS = SOC 2 path, custom
domain, marketplace billing. GovCloud = legally required for IL2+ (CUI). Code
is portable; compliance posture isn't.

**Render upgrade today (10 min, $7/mo):**
- Move from free → paid tier (kills 30s cold starts, persistent disk for
  SQLite + audit log)
- Custom domain (`ionshield.app`)
- TLS auto-renew (Render handles)

### 9.2 Government readiness — gap analysis

The code does what it says. The *paperwork* is what makes it deployable to
gov.

#### Must-fix before any serious gov conversation (4–6 weeks, ~$0)

| Gap | What we have | What's needed | Effort |
|---|---|---|---|
| Container hardening | `python:3.12-slim`, non-root user | Distroless or Chainguard image, dropped capabilities | 2 days |
| Vulnerability scanning | None | Trivy + Grype in CI, fail on HIGH/CRIT CVEs | 1 day |
| Static analysis | ruff lint only | Bandit (Python sec) + Semgrep + CodeQL | 2 days |
| SBOM | None | Generate via `syft` per build, attach to release | 1 day |
| Container signing | Unsigned | Cosign + Sigstore (free) | 1 day |
| Secrets management | Render env vars | Document Vault/AWS SM migration path | 1 day |
| Admin endpoint MFA | Single Bearer | TOTP / WebAuthn on admin routes | 3 days |
| Audit log retention | "Forever, in SQLite" | Documented 7-year retention, immutable storage | 1 day |
| Incident response plan | None | Markdown runbook in repo, on-call defined | 2 days |
| DPA / privacy template | None | Standard DPA text, GDPR/CCPA flow | 2 days |

#### Phase-2 (3–6 mo, ~$15–30k)

- **SOC 2 Type I** via Drata / Vanta — required for commercial regulated
  buyers (aviation, ag co-op, insurer)
- **CMMC Level 1 self-assessment** — basic FAR 52.204-21, free
- **Penetration test** — ~$10–25k from a NIST-approved 3PAO

#### Phase-3 (6–18 mo, ~$300k–$1M)

- **FedRAMP Tailored / Low** via AWS GovCloud + 3PAO — sponsorship needed
- **CMMC Level 2** third-party assessed — required for handling CUI on DoD
- **IL4 ATO sponsorship** from a DoD program (typically follows SBIR Phase II)

#### What I'd actually do this week

1. STIG-ish container hardening + Trivy + Bandit + Semgrep + SBOM + cosign
   in CI — all free, all this week
2. Write the IRP, DPA, and retention policy markdowns — pre-empts every
   procurement review
3. Move Render to paid tier, custom domain
4. Tag a `v1.0.0` release with signed image + SBOM attached — first artifact
   procurement can actually evaluate

### 9.3 Feature roadmap (ROI-ranked)

Ranked by `(dollar size of problem) × (how uniquely we solve it) ÷ (eng cost)`.

#### Tier 1 — ship in next 30 days

**a. Per-asset alerts (push notifications + webhooks).** Operators register
their AOI / route / RTK base / fleet; we ping them when degradation is
*imminent for them specifically*. Turns the $0 free tier into a $200/mo paid
tier.

**b. Verticalised ag module — "RTK Today".** Ag lost ~$500M during Gannon.
Build a one-screen ag view: pick your county, today's autosteer reliability
score, best operating window in the next 24–72 h, mobile-first. John Deere /
Climate FieldView / Granular consume space-weather data — they don't model it.
We can be the source.

**c. UAV / BVLOS pre-flight check API.** Single endpoint
`POST /api/v3/preflight` — operator submits flight plan + platform → gets
GO / DELAY / NO-GO with the specific window. Plugs into Skydio Cloud, DJI
Enterprise, ArduPilot via partnerships.

#### Tier 2 — ship in 60–90 days

**d. Recommendations API (not just data).** Return
`{"action": "delay", "until": "...", "reason": "..."}` instead of raw numbers.
Operators want decisions, not data. Wraps existing engine; sells for 3× the
price.

**e. Insurance / parametric-trigger feed.** Sell to crop insurers, parametric
ag insurance products, and aviation underwriters. They need *attested* data
with provenance — which we already have (SHA-256 hashes, audit trail). B2B2C —
we don't acquire the farmer; we ride the insurer's distribution.

**f. Mobile app (iOS + Android).** Drone operators, surveyors, ag drivers do
not sit at desks. Capacitor or React Native around the existing dashboard.

**g. Bring-your-own-data ingestion.** Customer uploads RTK fix-time logs /
GPS C/N0 / HF SNR — we calibrate the model to their environment. This *is*
the moat — every month of customer data makes us harder to replace.

#### Tier 3 — ship in 6 mo

**h. ATAK plugin (real native, not just KML).** Wins SOCOM/SF deals.

**i. Threat-intel overlay.** Geomagnetic + GPS jamming + adversary spoofing
reports. Single pane of glass for *all* GNSS-degraded operations.

**j. Predictive fleet maintenance.** "47 RTK receivers across these farms
will see degradation Wednesday. Prioritise manual passes for fields 12–15."
Cross-fleet optimisation sells to enterprise ag co-ops at 10× price.

### 9.4 Pricing & packaging

| Tier | Audience | Price | What they get |
|---|---|---|---|
| **Free / Demo** | Anyone | $0 | Read-only dashboard, public scenarios, sim mode, basic API (rate-limited) |
| **Operator** | Solo ag, drone operators, surveyors | **$200–500/mo** | API key, alerts, mobile app, AOI persistence, daily digest |
| **Team** | UAV companies, ag co-ops, dispatch desks | **$5–25k/mo** | Multi-user, customer-tuned model, ATAK feed, SOC 2 access, SLA |
| **Enterprise** | Defense, insurance, large fleets | **Quote** | On-prem / GovCloud, FedRAMP, IL4, custom integrations, dedicated SE |

The Operator tier is what converts the demo. Missing pieces: self-serve
billing (Stripe), per-key rate limits (we have the infra, not the metering),
email/SMS alerts.

### 9.5 Wedge-customer strategy

Don't sell to "defense, ag, aviation, drone, maritime" all at once. Pick ONE
for the first 6 months.

**My pick: precision agriculture (specifically RTK / autosteer).**

Why:
- $500M loss in May 2024 is real and recent — board-level pain
- Procurement is fast (a co-op manager has a P-card, not a contracting officer)
- Distribution: John Deere Operations Center API, Climate FieldView, Granular,
  AgLeader integrations — partner channel beats direct sales
- Geographic concentration (Iowa, Illinois, Nebraska, Kansas + Brazil,
  Argentina, Ukraine)
- Insurers will pay regardless (parametric crop insurance is $1B+ market)

Defense is the **prestige play** but takes 18–36 months to first dollar. Ag
could pay in 60 days.

**Defense parallel track:** SBIR Phase I (AFWERX or SpaceWERX) — $75k
non-dilutive, 6-month timeline. One-week-of-writing.

### 9.6 Funding path

Current state = MVP that demos well, real physics, real ML, ATAK/Foundry
packs, 12 months of unsexy infra. Enough to raise $500k–$1.5M pre-seed.

**Non-dilutive (do these regardless):**

| Program | $ | Timeline | Effort |
|---|---|---|---|
| AFWERX SBIR Phase I | $75k | 3 mo | 1 week of writing |
| SpaceWERX | ~$75k | 3 mo | Same template |
| NSF SBIR Phase I (commercial track) | $275k | 6–9 mo | 2 weeks of writing |
| DoE (grid-resilience angle, GIC) | varies | 6–12 mo | 2 weeks |

**Dilutive (later):**
- **Pre-seed** — $500k–$1.5M. Defense-focused VCs (Shield Capital, America's
  Frontier Fund, In-Q-Tel, Razor's Edge) or vertical SaaS (USV, BCV) once 1
  paying customer.
- 2026 conversation, not now. **Get a paying customer first.**

### 9.7 30/60/90 day plan

#### Days 1–30 — security baseline + ag wedge MVP

- ✅ Render paid tier + custom domain (1 hr)
- ☐ Trivy + Bandit + Semgrep + SBOM + cosign in CI (1 week)
- ☐ IRP, DPA, retention markdowns committed (2 days)
- ☐ Tag v1.0.0 with signed image + SBOM (1 hr)
- ☐ Build "RTK Today" — ag-vertical view (2 weeks)
- ☐ Stripe-billed Operator tier with self-serve checkout (1 week)
- ☐ Per-AOI email alerts via SES (1 week)
- ☐ Submit AFWERX SBIR Phase I proposal (1 week of writing)

#### Days 31–60 — first paying customer + SOC 2 kickoff

- ☐ Drata or Vanta SOC 2 Type I engagement (~$15–20k)
- ☐ Mobile app — Capacitor wrap with push (3 weeks)
- ☐ First ag/UAV pilot — 5 customers at $200/mo, 3-mo commit
- ☐ Recommendations API — `POST /api/v3/preflight` (1 week)
- ☐ BYOD ingestion — customer-uploaded RTK logs feed feedback store (2 weeks)
- ☐ Aviation BVLOS partnership outreach (Skydio, Auterion, Cape, ArduPilot)

#### Days 61–90 — scale + government parallel track

- ☐ SOC 2 Type I issued, marketing it
- ☐ Insurance / parametric trigger pilot — pitch a crop insurer (Climate
  Corp's reinsurance arm, AgriLogic, Hudson Crop)
- ☐ ATAK native plugin work begins (hire or contract Android dev)
- ☐ SBIR Phase I award response — if won, kickoff
- ☐ GovCloud parallel deployment scaffolded (no live customers, just infra path)
- ☐ First commercial customer at $5k/mo Team tier (target)

### 9.8 Open questions

Three things to decide before a serious push:

1. **Wedge — ag, UAV/BVLOS, or defense first?** Default to ag unless we have
   a warm defense intro.
2. **Dilution comfort — pre-seed in next 6 mo, or grind to revenue first?**
3. **Federal commitment — willing to spend 6–12 months on FedRAMP/CMMC
   paperwork (essentially zero immediate revenue), or stay commercial-first?**

---

## 10. Key links

### Live URLs

| Surface | URL |
|---|---|
| Public demo | https://ionshield-demo-mvp.onrender.com |
| 3D Dashboard | https://ionshield-demo-mvp.onrender.com/dashboard |
| Simulation | https://ionshield-demo-mvp.onrender.com/simulation |
| Integrations hub | https://ionshield-demo-mvp.onrender.com/integrations |
| API console | https://ionshield-demo-mvp.onrender.com/api-console |
| ML page | https://ionshield-demo-mvp.onrender.com/ml |
| ATAK install | https://ionshield-demo-mvp.onrender.com/atak |
| Foundry install | https://ionshield-demo-mvp.onrender.com/foundry |
| Pricing | https://ionshield-demo-mvp.onrender.com/pricing |
| Use cases | https://ionshield-demo-mvp.onrender.com/use-cases |
| Health | https://ionshield-demo-mvp.onrender.com/api/v3/health |
| 24h Kp forecast | https://ionshield-demo-mvp.onrender.com/api/v3/forecast/kp |

### Repo / dev

- GitHub: https://github.com/jjwest3/IonShield_Demo_MVP
- CI: https://github.com/jjwest3/IonShield_Demo_MVP/actions
- Latest main commit: `738fd21` (Phase D revert)

### Key code paths

| Module | What it does |
|---|---|
| `app/data/noaa.py` | NOAA SWPC ingestion |
| `app/data/ustec.py` | NASA OMNI / GloTEC ingestion |
| `app/data/fusion.py` | 324-cell grid fusion |
| `app/data/foundry_sync.py` | Parquet writes to Foundry datasets |
| `app/data/api_keys.py` | Bearer-token auth + mint/lookup/revoke |
| `app/data/audit_log.py` | Per-request audit middleware |
| `app/models/risk.py` | Klobuchar, CCIR, Bailey PCA, Nakagami |
| `app/models/impact.py` | Per-region impact rows |
| `app/models/decision.py` | Route/comms decision engine |
| `app/models/ml_classifier.py` | Storm event classifier |
| `app/models/kp_forecaster.py` | 24h Kp ridge forecaster |
| `app/models/auto_pilot.py` | Champion/challenger A/B + auto-promote |
| `app/api/routes_v3.py` | Modern API surface |
| `app/outputs/atak.py` | ATAK network link + DDIL pack |
| `app/outputs/foundry_pack.py` | Foundry ontology + Workshop layout |
| `app/outputs/cot.py` | Cursor-on-Target XML feed |
| `app/outputs/earth_studio.py` | KML/KMZ + Earth Studio CSV |
| `frontend/src/components/Globe.jsx` | CesiumJS dashboard |
| `frontend/src/components/Panel.jsx` | Right-panel decision UI |
| `app/pages/*.html` | Marketing + product pages |
| `app/static/marketing.css` | Design system |

---

*End of document.*
