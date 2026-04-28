"""
Phase 3b — Foundry Workshop pack.

Generates Foundry-compatible ontology object definitions, sample SQL
queries, and a Workshop layout descriptor that a Foundry admin imports
to stand up an IonShield app inside their tenant.

What ships:
  - ontology_objects()  → JSON list of Object Type definitions (Region,
                          StormEvent, ImpactRow, ModelVersion) with the
                          property schemas Foundry expects.
  - sample_sql_queries() → SQL the user can paste into Foundry's SQL
                           console for each common analyst question.
  - workshop_layout()    → JSON descriptor for a starter Workshop module
                           with three views (live globe, storm history,
                           impact drill-in).

These are static descriptors; Foundry admins import them via the
Ontology Manager + Workshop UI rather than the dataset write path,
which is why we generate JSON not Parquet here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def ontology_objects() -> list[dict[str, Any]]:
    """
    Foundry Object Type definitions for the IonShield ontology.

    Foundry's Object Types are roughly: a primary key, a list of typed
    properties, and a backing dataset. Each entry below maps 1:1 to one
    of our Foundry datasets so the admin can wire the linkage in
    Ontology Manager without writing a transform.
    """
    return [
        {
            "apiName": "IonShieldRegion",
            "displayName": "Space Weather Region",
            "description": (
                "A cell in IonShield's 324-cell global grid (10° lat × 20° lon). "
                "Each region carries current GPS/HF/SATCOM/radar impact metrics."
            ),
            "primaryKey": "region_id",
            "backingDataset": {"label": "location_risk", "envVar": "FOUNDRY_LOCATION_RISK_RID"},
            "properties": [
                {"name": "region_id", "type": "STRING", "required": True, "description": "R{lat}{lon} canonical id"},
                {"name": "lat_deg", "type": "DOUBLE", "required": True},
                {"name": "lon_deg", "type": "DOUBLE", "required": True},
                {"name": "geomag_lat_deg", "type": "DOUBLE", "required": True},
                {"name": "kp", "type": "DOUBLE"},
                {"name": "tec_tecu", "type": "DOUBLE"},
                {"name": "gps_l1_error_m", "type": "DOUBLE", "description": "Klobuchar+Mannucci position error"},
                {"name": "hf_absorption_db", "type": "DOUBLE", "description": "CCIR-888 absorption"},
                {"name": "hf_blackout_probability", "type": "DOUBLE"},
                {"name": "satcom_l_fade_db", "type": "DOUBLE"},
                {"name": "fetched_at", "type": "TIMESTAMP", "required": True},
            ],
            "indexedProperties": ["region_id", "fetched_at", "kp"],
        },
        {
            "apiName": "IonShieldStormEvent",
            "displayName": "Storm Event",
            "description": "A detected geomagnetic / solar event with onset, peak, and end timestamps.",
            "primaryKey": "id",
            "backingDataset": {"label": "events", "envVar": "FOUNDRY_EVENTS_RID"},
            "properties": [
                {"name": "id", "type": "INTEGER", "required": True},
                {"name": "event_type", "type": "STRING", "description": "GEOMAG_MAIN, FLARE_X, SEP_EVENT, ..."},
                {"name": "state", "type": "STRING", "description": "ONSET, PEAK, ENDED"},
                {"name": "severity", "type": "STRING", "description": "G0..G5 / R1..R3"},
                {"name": "region_id", "type": "STRING", "description": "Region of impact (or GLOBAL)"},
                {"name": "t_onset", "type": "TIMESTAMP", "required": True},
                {"name": "t_peak", "type": "TIMESTAMP"},
                {"name": "t_end", "type": "TIMESTAMP"},
                {"name": "driver", "type": "STRING"},
                {"name": "peak_value", "type": "DOUBLE"},
                {"name": "rationale", "type": "STRING"},
                {"name": "classifier", "type": "STRING"},
                {"name": "confidence", "type": "DOUBLE"},
            ],
            "indexedProperties": ["t_onset", "event_type", "severity"],
            "relations": [
                {"toApiName": "IonShieldRegion", "via": "region_id", "type": "MANY_TO_ONE"},
            ],
        },
        {
            "apiName": "IonShieldImpactRow",
            "displayName": "Impact Row",
            "description": "Per-system impact metric (GPS/HF/SATCOM/radar) at a region for a given time.",
            "primaryKey": ["region_id", "when_utc", "system", "metric"],
            "backingDataset": {"label": "impact", "envVar": "FOUNDRY_IMPACT_RID"},
            "properties": [
                {"name": "region_id", "type": "STRING", "required": True},
                {"name": "when_utc", "type": "TIMESTAMP", "required": True},
                {"name": "system", "type": "STRING", "description": "GPS, HF, SATCOM, RADAR"},
                {"name": "subsystem", "type": "STRING", "description": "L1, L2, ..."},
                {"name": "metric", "type": "STRING", "description": "position_error_m, absorption_db, ..."},
                {"name": "value", "type": "DOUBLE", "required": True},
            ],
            "indexedProperties": ["region_id", "when_utc", "system"],
            "relations": [
                {"toApiName": "IonShieldRegion", "via": "region_id", "type": "MANY_TO_ONE"},
            ],
        },
        {
            "apiName": "IonShieldRawObservation",
            "displayName": "Raw Space Weather Observation",
            "description": "5-min cadence NOAA SWPC + GloTEC fetch — the ground truth for every other object.",
            "primaryKey": ["fetched_at", "fetch_source"],
            "backingDataset": {"label": "space_weather_raw", "envVar": "FOUNDRY_SPACE_WEATHER_RAW_RID"},
            "properties": [
                {"name": "fetched_at", "type": "TIMESTAMP", "required": True},
                {"name": "fetch_source", "type": "STRING", "required": True},
                {"name": "kp_index", "type": "DOUBLE"},
                {"name": "bz_nt", "type": "DOUBLE"},
                {"name": "xray_flux_wm2", "type": "DOUBLE"},
                {"name": "proton_flux_10mev_pfu", "type": "DOUBLE"},
                {"name": "wind_speed_km_s", "type": "DOUBLE"},
                {"name": "f107_sfu", "type": "DOUBLE"},
                {"name": "glotec_median_tecu", "type": "DOUBLE"},
            ],
            "indexedProperties": ["fetched_at"],
        },
    ]


def sample_sql_queries() -> list[dict[str, str]]:
    """SQL ready to paste into Foundry's SQL console — one per common analyst question."""
    return [
        {
            "name": "Last hour Kp envelope",
            "description": "Min / mean / max Kp over the most recent hour.",
            "sql": (
                "SELECT MIN(kp_index) AS kp_min, AVG(kp_index) AS kp_mean, MAX(kp_index) AS kp_max\n"
                "FROM space_weather_raw\n"
                "WHERE fetched_at >= NOW() - INTERVAL 1 HOUR;"
            ),
        },
        {
            "name": "Top 10 worst-impacted regions right now",
            "description": "Regions ranked by combined GPS + HF severity in the latest snapshot.",
            "sql": (
                "WITH latest AS (\n"
                "  SELECT MAX(fetched_at) AS t FROM location_risk\n"
                ")\n"
                "SELECT region_id, lat_deg, lon_deg, gps_l1_error_m, hf_absorption_db,\n"
                "       (gps_l1_error_m * 0.5 + hf_absorption_db * 0.05) AS combined_score\n"
                "FROM location_risk, latest\n"
                "WHERE fetched_at = latest.t\n"
                "ORDER BY combined_score DESC\n"
                "LIMIT 10;"
            ),
        },
        {
            "name": "Storm events in the last 30 days",
            "description": "All ONSET / ENDED events grouped by type and severity.",
            "sql": (
                "SELECT event_type, severity, COUNT(*) AS n,\n"
                "       MIN(t_onset) AS first_seen, MAX(t_onset) AS last_seen\n"
                "FROM events\n"
                "WHERE t_onset >= NOW() - INTERVAL 30 DAY\n"
                "GROUP BY event_type, severity\n"
                "ORDER BY n DESC;"
            ),
        },
        {
            "name": "GPS L1 outage hours by region",
            "description": "Hours where GPS L1 error exceeded 10 m, by region, last 7 days.",
            "sql": (
                "SELECT region_id, COUNT(*) AS outage_hours\n"
                "FROM impact\n"
                "WHERE system = 'GPS' AND metric = 'position_error_m'\n"
                "  AND value > 10\n"
                "  AND when_utc >= NOW() - INTERVAL 7 DAY\n"
                "GROUP BY region_id\n"
                "ORDER BY outage_hours DESC\n"
                "LIMIT 20;"
            ),
        },
        {
            "name": "Latest model version + champion accuracy",
            "description": "What's the active classifier and how well is it doing?",
            "sql": (
                "SELECT version, trained_at, n_train_samples, training_source,\n"
                "       rmse_per_horizon\n"
                "FROM space_weather_raw\n"
                "WHERE fetch_source = 'model_registry'\n"
                "ORDER BY fetched_at DESC LIMIT 1;"
            ),
        },
    ]


def workshop_layout() -> dict[str, Any]:
    """
    Workshop module descriptor — three tabs the admin pastes into the
    Workshop config: live globe, storm history, region drill-in.

    Foundry's Workshop format evolves; treat this as a layout-intent
    document the admin uses as a spec rather than an importable bundle.
    """
    return {
        "moduleId": "ionshield-workshop",
        "title": "IonShield — Space Weather Operations",
        "description": "Live + historical space weather intelligence, powered by IonShield datasets.",
        "tabs": [
            {
                "id": "live",
                "title": "Live Globe",
                "widgets": [
                    {
                        "type": "GeoMap",
                        "title": "Current risk grid",
                        "objectType": "IonShieldRegion",
                        "filter": {"property": "fetched_at", "op": "MAX"},
                        "colorBy": "hf_absorption_db",
                        "colorScale": [
                            {"max": 5, "color": "#22c55e"},
                            {"max": 10, "color": "#fbbf24"},
                            {"max": 20, "color": "#f59e0b"},
                            {"max": 30, "color": "#ef4444"},
                            {"max": 999, "color": "#dc2626"},
                        ],
                        "tooltipProperties": ["region_id", "kp", "gps_l1_error_m", "hf_absorption_db"],
                    },
                    {
                        "type": "MetricCards",
                        "title": "Drivers",
                        "source": "IonShieldRawObservation",
                        "filter": {"property": "fetched_at", "op": "MAX"},
                        "metrics": [
                            {"label": "Kp", "property": "kp_index", "thresholds": {"warn": 5, "alert": 7}},
                            {"label": "Bz (nT)", "property": "bz_nt"},
                            {"label": "Wind (km/s)", "property": "wind_speed_km_s"},
                            {"label": "X-ray (W/m²)", "property": "xray_flux_wm2"},
                        ],
                    },
                ],
            },
            {
                "id": "history",
                "title": "Storm History",
                "widgets": [
                    {
                        "type": "Timeline",
                        "title": "Detected events",
                        "objectType": "IonShieldStormEvent",
                        "timeProperty": "t_onset",
                        "groupBy": "event_type",
                        "colorBy": "severity",
                        "rangePresets": ["1d", "7d", "30d", "1y"],
                    },
                    {
                        "type": "Table",
                        "title": "Events",
                        "objectType": "IonShieldStormEvent",
                        "columns": ["t_onset", "event_type", "severity", "region_id", "rationale", "classifier"],
                        "sort": [{"property": "t_onset", "order": "DESC"}],
                    },
                ],
            },
            {
                "id": "drilldown",
                "title": "Region Drill-in",
                "widgets": [
                    {
                        "type": "ObjectPicker",
                        "title": "Region",
                        "objectType": "IonShieldRegion",
                        "binding": "selectedRegion",
                    },
                    {
                        "type": "TimeSeries",
                        "title": "GPS L1 error (24 h)",
                        "objectType": "IonShieldImpactRow",
                        "filter": [
                            {"property": "region_id", "op": "EQ", "binding": "selectedRegion"},
                            {"property": "system", "op": "EQ", "value": "GPS"},
                            {"property": "metric", "op": "EQ", "value": "position_error_m"},
                            {"property": "when_utc", "op": "AFTER", "value": "NOW-24H"},
                        ],
                        "yAxis": "value",
                    },
                    {
                        "type": "TimeSeries",
                        "title": "HF absorption (24 h)",
                        "objectType": "IonShieldImpactRow",
                        "filter": [
                            {"property": "region_id", "op": "EQ", "binding": "selectedRegion"},
                            {"property": "system", "op": "EQ", "value": "HF"},
                        ],
                        "yAxis": "value",
                    },
                ],
            },
        ],
    }


def build_pack() -> dict[str, Any]:
    """Build the entire Foundry pack as a single JSON document."""
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ontology_objects": ontology_objects(),
        "sample_sql_queries": sample_sql_queries(),
        "workshop_layout": workshop_layout(),
        "datasets": {
            "space_weather_raw": {
                "env_var": "FOUNDRY_SPACE_WEATHER_RAW_RID",
                "format": "parquet",
                "cadence": "5 min",
                "purpose": "Raw NOAA SWPC + GloTEC merged feed; ground truth for every other dataset.",
            },
            "location_risk": {
                "env_var": "FOUNDRY_LOCATION_RISK_RID",
                "format": "parquet",
                "cadence": "5 min",
                "purpose": "Per-region (324 cells × time) impact metrics.",
            },
            "events": {
                "env_var": "FOUNDRY_EVENTS_RID",
                "format": "parquet",
                "cadence": "event-driven",
                "purpose": "Detected storm onsets, SSCs, X-flares; one row per state transition.",
            },
            "impact": {
                "env_var": "FOUNDRY_IMPACT_RID",
                "format": "parquet",
                "cadence": "5 min",
                "purpose": "Long form (region × time × system × metric) — best for time-series analytics.",
            },
        },
    }


def to_json(pack: dict[str, Any] | None = None) -> str:
    return json.dumps(pack or build_pack(), indent=2)
