# IonShield Dashboard — Operator Guide

The dashboard is a React single-page application served at `/dashboard` (or `/`).  
It uses **CesiumJS** for the 3D globe and talks directly to the FastAPI backend on the same origin.

---

## Quick start

```bash
# 1. Install frontend deps (first time only)
cd frontend && npm install

# 2. Start the backend with hot-reload
uvicorn app.main:app --reload --port 8000

# 3. (Optional) Start the Vite dev server for instant frontend HMR
cd frontend && npm run dev          # http://localhost:5173

# 4. Open the dashboard
open http://localhost:8000/dashboard   # served by FastAPI (production build)
# — or —
open http://localhost:5173             # Vite dev server (faster iteration)
```

---

## Layout

```
┌──────────────────────── Header (48 px) ────────────────────────────────┐
│  Logo  •  Risk badge  •  Kp  •  X-ray  •  Bz  •  Solar wind  •  Help  │
├──────────────────────────────────────────────────┬─────────────────────┤
│                                                  │                     │
│             3D CesiumJS Globe                    │   Operator Panel    │
│          (click to place waypoints)              │    (420 px wide)    │
│                                                  │                     │
│  ┌─ Replay Drawer (left, 300 px) ─┐              │  Layer toggles      │
│  │  archived NOAA snapshots       │              │  Waypoint builder   │
│  └────────────────────────────────┘              │  Platform picker    │
│                                                  │  Decision result    │
│  ┌─ Elevation / Risk Profile (bottom) ──────────┐│                     │
│  │  ROUTE PROFILE  ████░░████░░  340 km         ││                     │
│  └──────────────────────────────────────────────┘│                     │
│  ┌─ Forecast Timeline (bottom) ─────────────────┐│                     │
│  │  72-hour Kp forecast windows (bar chart)     ││                     │
│  └──────────────────────────────────────────────┘│                     │
└──────────────────────────────────────────────────┴─────────────────────┘
```

---

## 3D Globe

The globe is powered by **CesiumJS** and renders on a WebGL canvas.

| Action | Result |
|--------|--------|
| Click **➕ Click to place WP** | Enters click mode; next globe click drops a waypoint |
| Click anywhere on globe (in click mode) | Places a waypoint; camera auto-flies to it |
| Scroll wheel / pinch | Zoom in/out |
| Left-drag | Orbit / pan |
| Right-drag | Tilt |
| ⊙ button (after waypoints placed) | Fit camera to route bounding sphere |

### Imagery & terrain

By default the globe uses **OpenStreetMap** raster tiles (free, no token needed).

To upgrade to **Bing Maps Aerial** high-resolution imagery, set a Cesium Ion access
token in `frontend/.env.local`:

```bash
# frontend/.env.local  (git-ignored — never commit)
VITE_CESIUM_TOKEN=your_token_here
```

Get a free token at <https://ion.cesium.com/>.  
See [`docs/cesium-token.md`](cesium-token.md) for full setup instructions.

---

## Header — Solar driver chips

| Chip | NOAA source | Meaning |
|------|-------------|---------|
| **Kp** | SWPC 1-min | Planetary geomagnetic index (0–9). ≥ 5 = storm |
| **X-ray** | GOES | Solar X-ray flux class (A/B/C/M/X) |
| **Bz** | ACE/DSCOVR | IMF Bz component (nT). Negative = geoeffective |
| **Solar wind** | ACE/DSCOVR | Proton speed (km/s) |

The **data age** indicator turns amber when the last NOAA fetch is > 10 min old.

---

## Operator Panel

### Layer toggles

Controls which data overlays appear on the globe:

| Layer | Description |
|-------|-------------|
| **TEC Bands** | Ionospheric electron content risk zones (colour-coded by Kp) |
| **GPS Error** | Estimated GPS horizontal error bubble at each waypoint |
| **HF Skip** | HF radio skip-zone geometry for the current solar conditions |
| **Locations** | Named monitoring sites loaded from `locations.json` |

### Waypoint builder

Enter **lat / lon** (decimal degrees) manually and click **+ Add**, or use
**➕ Click to place WP** for interactive globe placement.

- **Remove** a single waypoint with the × button
- **Clear All** removes every waypoint
- Camera auto-flies to a placed waypoint (single) or fits the route (multiple)

### Platform picker

Select the asset type operating the route. Pre-sets adjust `asset_type` and
`criticality` sent to the decision engine:

| Platform | Asset type | Criticality |
|----------|-----------|------------|
| HMMWV | GPS L1 | 3 |
| LMTV | GPS L1 | 2 |
| MRAP | GPS L1/L2 | 4 |
| Rotary wing | GPS L1/L2 | 4 |
| Fixed wing | GPS L1/L5 | 4 |
| Dismounted | GPS L1 | 2 |
| Maritime | GPS/INS | 3 |

### Route Decision

Click **▶ Get Route Decision** to run the v2 typed decision engine.  
The response includes:

| Field | Meaning |
|-------|---------|
| **Action badge** | Machine-readable decision: `PROCEED`, `CAUTION`, `NO_GO`, … |
| **Action sentence** | Plain-English rationale |
| **Confidence** | Score 0–1 with label HIGH / MEDIUM / LOW / VERY_LOW |
| **Driver breakdown** | Factors that raised (+) or lowered (−) confidence |
| **Impacts** | Per-system effect estimates (GPS error, HF viability, SATCOM fade) |
| **Valid until** | UTC expiry of this assessment |
| **Provenance** | SHA-256 input hash, model version, observations used |

---

## Forecast timeline

The bar chart at the bottom of the globe shows the **72-hour Kp forecast** from
NOAA SWPC in 3-hour windows. Click a window to "scrub" to that forecast scenario —
TEC bands and the decision engine both re-compute using the forecast Kp.  
Click the active window (or click away) to return to live data.

---

## Elevation / Risk profile

The horizontal bar below the globe shows the route divided into segments, each
coloured by the worst risk level of its two endpoint waypoints:

| Colour | Risk level |
|--------|-----------|
| Blue | No data / NOMINAL |
| Green | NOMINAL |
| Yellow | ELEVATED |
| Orange | CAUTION |
| Red | DEGRADED / SEVERE |

> **Phase 1 note:** elevation values are currently flat (0 m). Real terrain
> sampling via `Cesium.sampleTerrain` is planned for Phase 2 and requires a
> Cesium Ion token.

---

## Replay drawer

Click **⏪ Replay** in the header to open the snapshot archive. Each row is a
saved NOAA observation. Selecting a snapshot:

1. Locks the globe to that historical dataset
2. Auto-runs the current route against the archived space-weather conditions
3. Shows a **"REPLAY"** banner in the decision panel

Click **↩ Back to Live** to resume real-time data.

---

## Stale data warning

If NOAA data is more than 10 minutes old, the decision panel shows a
**red STALE DATA banner**. Confidence is automatically penalised (visible as a
negative `data_freshness` driver in the breakdown).

Do not act on a stale decision without confirming current conditions via
[NOAA SWPC](https://www.swpc.noaa.gov/).

---

## API key authentication

If the backend is running with `API_KEY` set, every request requires an
`X-API-Key` header. Click the **🔑** icon in the header to enter or update the
key; it is stored in `localStorage` and never sent to a third party.

---

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Escape` | Close Help modal / exit click mode |
| `?` | Open Help modal |

---

## Confidence score reference

| Label | Score | Meaning |
|-------|-------|---------|
| HIGH | ≥ 0.75 | Fresh data, all NOAA feeds live. High trust. |
| MEDIUM | 0.55 – 0.74 | Minor data gaps or slightly stale. Use with awareness. |
| LOW | 0.40 – 0.54 | Missing feeds or stale data. Treat as advisory only. |
| VERY_LOW | < 0.40 | Significant data gaps. Cross-check before action. |

---

## Provenance

Expand **Provenance** in any decision result to see:

- **Model version** — which engine produced the result
- **Input hash** — SHA-256 fingerprint; same inputs always produce the same hash
- **Computed at** — UTC timestamp when the decision was made
- **Observations used** — which NOAA feeds were consumed
- **Forecasts used** — 24-hour Kp forecast value (if available)
- **Feeds offline** — any NOAA feeds that were unavailable

---

## Performance notes

- The Cesium globe uses `requestRenderMode: true` — only redraws on data
  changes or camera movement, saving ~100 % idle GPU.
- The Page Visibility API pauses NOAA polling when the tab is hidden.
- `suspendEvents / resumeEvents` batch Cesium entity updates to avoid thrashing.
