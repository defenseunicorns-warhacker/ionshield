# IonShield Dashboard — Pilot Guide

The dashboard is a single-page app served at `/dashboard` (or `/`).  
It talks directly to the FastAPI backend running on the same origin.

---

## Quick start

```bash
# 1. Start the backend
uvicorn app.main:app --reload

# 2. Open the dashboard
open http://localhost:8000/dashboard
```

No build step. No npm. The page loads Tailwind from CDN and Leaflet from unpkg.

---

## Tab overview

| Tab | What it does | Backend endpoint |
|-----|--------------|-----------------|
| **Location Risk** | Single-point GPS/HF/SATCOM assessment | `GET /api/risk/location` |
| **Route** | Multi-waypoint risk table (classic model) | `POST /api/risk/route` |
| **Forecast** | 72-hour Kp timeline + operational windows | `GET /api/forecast` |
| **Decision ★** | Typed v2 decisions with confidence + provenance | `/api/v2/*` |

---

## Decision tab (v2 engine)

The **Decision ★** tab is the primary pilot-facing interface for the typed
decision engine introduced in Slice 2.

### Mode: COMMS LINK

Gets an HF / SATCOM comms recommendation for a single observer position.

1. Enter **Observer Lat / Lon**.
2. Optionally expand *Destination coords* and enter a far-end position — this
   improves HF link geometry scoring.
3. Click **Get Comms Decision**.

**What you see in the result:**

| Field | Meaning |
|-------|---------|
| Action badge | One of: USE PRIMARY HF · USE ALTERNATE HF · SWITCH TO SATCOM · SWITCH TO UHF · DEGRADED MODE · HF NOT VIABLE |
| Action sentence | Plain-English rationale for the recommended action |
| Also consider | Alternative actions if conditions change |
| Recommended actions | Specific operator steps |
| **CONFIDENCE** bar | Score 0–1 with label HIGH / MEDIUM / LOW / VERY_LOW |
| Driver breakdown | Each factor that raised (+) or lowered (−) confidence |
| **IMPACTS** | Per-system metrics (HF absorption, GPS error, SATCOM fade, etc.) |
| **Provenance** | Model version, SHA-256 input hash, observations used, feeds offline |

### Mode: ROUTE RISK

Gets a GO / ADVISORY / CAUTION / NO-GO decision for a multi-waypoint route.

1. Switch to the **Route** tab and add waypoints (manually or by clicking the map).
2. Return to the **Decision ★** tab and click **ROUTE RISK**.
3. Select the platform type.
4. Click **Get Route Decision**.

The result includes the overall action + sentence, confidence, and a per-waypoint
table showing risk level, GPS error, and HF viability.

---

## Stale data warning

If the NOAA observation data is more than 10 minutes old, the decision panel
shows a **red STALE DATA banner** at the top of the result.  Confidence is
automatically penalised (visible in the driver breakdown as a negative
`data_freshness` or `stale_penalty` entry).

Do not act on a stale decision without confirming current conditions via
[NOAA SWPC](https://www.swpc.noaa.gov/).

---

## Replay a past decision

The **↩ REPLAY FROM ARCHIVE** section lets you re-run a decision against a
stored NOAA observation snapshot.  The replay endpoint guarantees that the same
snapshot always produces the same `input_hash` — i.e. the decision is
deterministic and auditable.

1. Enter a **Snapshot ID** (from `GET /api/v2/snapshots`) **or** a UTC
   timestamp in ISO-8601 format (`2024-05-11T18:00:00Z`).
2. Leave both blank to replay the most recent archived snapshot.
3. Click **↩ Replay Decision**.

The result shows a **blue REPLAY banner** identifying which snapshot was used
(ID, timestamp, source, Kp at time of snapshot).

To list available snapshots:
```bash
curl http://localhost:8000/api/v2/snapshots
```

---

## API key (auth)

If the backend was started with `API_KEY=<secret>` configured, every request
requires an `X-API-Key` header.

1. Click the **⚙** icon in the header (top-right corner).
2. Enter your API key and click **Save**.
3. The key is stored in browser `localStorage` and sent automatically on every
   request.  It is never sent to any third party.

If you get a `401 Unauthorized` error in the decision panel, click the
**Set API key ⚙** link in the error message.

To run in open mode (no auth), leave the API key field blank.

---

## Confidence — what the score means

| Label | Score | Meaning |
|-------|-------|---------|
| HIGH | ≥ 0.75 | Fresh data, all NOAA feeds live. High trust. |
| MEDIUM | 0.55 – 0.74 | Minor data gaps or slightly stale. Use with awareness. |
| LOW | 0.40 – 0.54 | Missing feeds or stale data. Treat as advisory only. |
| VERY_LOW | < 0.40 | Significant data gaps. Cross-check before action. |

The **driver breakdown** lists each factor that raised (+) or penalised (−)
the score, with a plain-English explanation.

---

## Provenance accordion

Expand the **Provenance** section at the bottom of any decision result to see:

- **Model version** — which engine produced the result
- **Input hash** — SHA-256 fingerprint; same inputs always produce the same hash
- **Computed at** — UTC timestamp when the decision was made
- **Observations used** — which NOAA feeds were read
- **Forecasts used** — 24-hour Kp forecast value (if available)
- **Feeds offline** — any NOAA feeds that were unavailable

---

## Manual test checklist

| Test | Expected result |
|------|----------------|
| Enter lat=38.8, lon=-77.0 → Get Comms Decision | Result panel appears with action badge + sentence |
| Check confidence bar | Bar fills proportionally; label shows HIGH/MEDIUM/LOW |
| Open Provenance accordion | input_hash, model_version, observations_used visible |
| Add waypoints in Route tab, switch to Decision tab | "N waypoints loaded" banner appears (blue) |
| Click Get Route Decision | Per-waypoint table with risk level + GPS error |
| Set no waypoints, click Get Route Decision | Error: "Add waypoints in the Route tab first" |
| Enter invalid lat (200) → Get Comms Decision | Error: "Coordinates out of range" |
| Replay with snapshot_id=9999 → Replay | 404 error displayed |
| Click ⚙ gear → enter API key → Save | "API key saved ✓" feedback shown |
| Kill backend, refresh page | Header shows "Refresh failed" (no crash) |
| Data age > 10 min (wait or use replay) | Red STALE DATA banner appears in result |
