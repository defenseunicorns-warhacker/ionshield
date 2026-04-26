# IonShield API v2 — Decision Engine

> **Status:** Merge-ready vertical slice. Endpoints are live at `/api/v2/`.

## Overview

v2 adds a typed Decision Engine layer on top of the existing geophysical risk engine.
Instead of raw numbers, you get structured `RecommendationObject` responses with:

- **action** — machine-readable decision (e.g. `HF_NOT_VIABLE`, `NO_GO`)
- **action_sentence** — plain-English rationale for operators
- **confidence** — score (0–1) + named penalty drivers (freshness, completeness, Bz variability)
- **provenance** — SHA-256 hash of all inputs, enabling replay verification

---

## Running locally

```bash
# From repo root
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Interactive docs: <http://localhost:8000/docs>

---

## Endpoints

### `GET /api/v2/comms-decision`

Recommend an HF communications configuration for a single link.

**Query parameters**

| Param      | Required | Type   | Description                              |
|------------|----------|--------|------------------------------------------|
| `lat`      | ✓        | float  | Observer latitude (−90 to 90)            |
| `lon`      | ✓        | float  | Observer longitude (−180 to 180)         |
| `dest_lat` |          | float  | Link destination latitude (defaults to lat) |
| `dest_lon` |          | float  | Link destination longitude (defaults to lon)|

**Example request**

```
GET /api/v2/comms-decision?lat=45.0&lon=-10.0&dest_lat=55.0&dest_lon=-30.0
```

**Example response (trimmed)**

```json
{
  "id": "b3f2a1c0-...",
  "decision_type": "COMMS_FALLBACK",
  "action": "USE_PRIMARY_HF",
  "action_sentence": "USE PRIMARY HF — 14.5 MHz at 82% reliability. Conditions favorable.",
  "valid_until": "2024-05-11T01:00:00+00:00",
  "alternatives": ["USE_ALTERNATE_HF"],
  "impacts": [...],
  "recommended_actions": ["Conditions nominal. Primary HF recommended."],
  "confidence": {
    "score": 0.95,
    "label": "HIGH",
    "stale_data": false,
    "stale_penalty_applied": false,
    "data_completeness": 1.0,
    "drivers": [],
    "computed_at": "2024-05-11T00:00:00+00:00"
  },
  "provenance": {
    "model_version": "1.0.0",
    "input_hash": "sha256:a3f9...",
    "computed_at": "2024-05-11T00:00:00+00:00",
    "observations_used": ["kp_index", "bz_gsm_nt", "xray_flux_wm2", "proton_flux_10mev_pfu", "solar_wind_km_s"],
    "forecasts_used": [],
    "feeds_unavailable": [],
    "operator_overrides": []
  },
  "operator_ack": false,
  "operator_note": "",
  "created_at": "2024-05-11T00:00:00+00:00"
}
```

**Action values**

| Action             | Meaning                                                    |
|--------------------|------------------------------------------------------------|
| `USE_PRIMARY_HF`   | Best band ≥75% reliability — use normal comms              |
| `USE_ALTERNATE_HF` | Best band 50–74% — conditions degraded but usable          |
| `DEGRADED_MODE`    | Best band 25–49% — marginal; prefer SATCOM if available    |
| `HF_NOT_VIABLE`    | No bands viable, or PCA active — switch to SATCOM/UHF      |
| `SWITCH_TO_SATCOM` | Explicit SATCOM fallback (appears in `alternatives`)        |
| `SWITCH_TO_UHF`    | Explicit UHF fallback (appears in `alternatives`)           |

---

### `POST /api/v2/route-decision`

Assess risk for each waypoint and produce a route-level GO/ADVISORY/CAUTION/NO-GO recommendation.

**Request body**

```json
{
  "waypoints": [
    {"lat": 62.1, "lon": -28.4, "name": "MODOG"},
    {"lat": 67.3, "lon": -18.2, "name": "MIMKU"},
    {"lat": 71.8, "lon": -8.1,  "name": "GUNSO"}
  ],
  "platform": {
    "asset_type": "GPS_L1",
    "criticality": 4,
    "system_dependencies": [
      {
        "system_type": "HF",
        "primary_freqs_mhz": [8.0, 14.0],
        "fallback_modes": ["SATCOM", "UHF"],
        "degradation_tolerance": 2
      }
    ]
  }
}
```

**Platform fields**

| Field        | Type | Default | Description                                 |
|--------------|------|---------|---------------------------------------------|
| `asset_type` | str  | GPS_L1  | GPS_L1 \| GPS_L1L2 \| GPS_L1L5 \| GPS_INS \| SBAS |
| `criticality`| int  | 3       | 1=lowest … 5=highest; raises NO-GO threshold by 5 pts per tier above 3 |

**Example response (trimmed)**

```json
{
  "id": "...",
  "decision_type": "ROUTE_RISK",
  "action": "NO_GO",
  "action_sentence": "NO-GO — GUNSO at SEVERE (score 82/100, GPS error 24.3 m). Postpone or re-route.",
  "valid_until": "2024-05-11T03:00:00+00:00",
  "confidence": {"score": 0.85, "label": "HIGH", ...},
  "provenance": {"input_hash": "sha256:...", ...},
  "waypoints": [
    {
      "name": "MODOG",
      "lat": 62.1, "lon": -28.4,
      "risk_level": "ELEVATED",
      "risk_score": 38.5,
      "gps_error_m": 7.2,
      "hf_viable": true,
      "hf_best_freq_mhz": 14.5,
      "hf_best_reliability_pct": 61.0,
      "hf_absorption_db": 6.2,
      "satcom_fade_db": 1.1,
      "s4_index": 0.15,
      "pca_active": false,
      "watch_notes": []
    }
  ]
}
```

**Action values**

| Action     | Score threshold | Meaning                                      |
|------------|-----------------|----------------------------------------------|
| `GO`       | < 20            | All waypoints nominal                        |
| `ADVISORY` | 20–39           | Elevated risk; monitor conditions            |
| `CAUTION`  | 40–59           | Degraded conditions; consider delay or backup|
| `NO_GO`    | ≥ 60            | Severe risk; postpone or re-route            |

*Thresholds are lowered by 5 pts per criticality tier above 3 (e.g. criticality=5 triggers NO-GO at score ≥ 50).*

---

## Confidence scoring

| Score   | Label    | Meaning                                         |
|---------|----------|-------------------------------------------------|
| ≥ 0.85  | HIGH     | Fresh data, all feeds available                 |
| 0.65–0.84 | MEDIUM | Minor freshness or completeness penalty         |
| 0.40–0.64 | LOW    | Stale data (>30 min) or multiple missing feeds  |
| < 0.40  | VERY_LOW | Severely degraded — treat as indicative only    |

Named penalty drivers are included in `confidence.drivers` so operators understand *why* confidence is reduced.

---

## Provenance and replay

The `provenance.input_hash` is a SHA-256 of the canonical JSON of all geophysical inputs that drove the decision (Kp, Bz, X-ray flux, proton flux, wind speed, forecast). Given the same inputs, the hash is identical — enabling post-mission replay verification.

```python
# Verify a decision was produced from known inputs
import hashlib, json

inputs = {"model_version": "1.0.0", "kp": 8.3, "bz_nt": -25.0, ...}
expected = "sha256:" + hashlib.sha256(
    json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert stored_hash == expected
```

---

## Running tests

```bash
pytest tests/test_decision.py -v
# or run the full suite
pytest tests/ -v
```

---

## Architecture note

The Decision Engine (`app/models/decision.py`) is fully stateless — it performs no NOAA I/O.
All geophysical state is passed via `EnvironmentSnapshot`, built in `routes_v2.py` from the
live NOAA cache. This means the engine can be unit-tested without network access and
supports deterministic replay by injecting historical snapshots.
