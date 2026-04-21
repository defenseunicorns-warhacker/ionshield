"""
GeoJSON FeatureCollection generator.

Produces risk zones (Polygon features) and installation placemarks (Point features)
compatible with Leaflet, Mapbox, deck.gl, and CoT/GeoJSON-based ATAK overlays.

Fill colours follow standard traffic-light risk convention:
  NOMINAL   → #10B981 (green)
  ELEVATED  → #F59E0B (amber)
  DEGRADED  → #F97316 (orange)
  SEVERE    → #EF4444 (red)
"""

from datetime import datetime, timezone

from app.data.noaa import get_kp
from app.models.risk import compute_risk, BASES

LAT_BANDS = [
    ("Polar North", 70, 90),
    ("Sub-Auroral North", 55, 70),
    ("Mid-Latitude North", 25, 55),
    ("Equatorial", -25, 25),
    ("Mid-Latitude South", -55, -25),
    ("Sub-Auroral South", -70, -55),
    ("Polar South", -90, -70),
]

_RISK_FILL: dict[str, str] = {
    "NOMINAL": "#10B981",
    "ELEVATED": "#F59E0B",
    "DEGRADED": "#F97316",
    "SEVERE": "#EF4444",
}


def generate_geojson() -> dict:
    """Return a GeoJSON FeatureCollection with zone polygons and base points."""
    kp = get_kp()
    now = datetime.now(timezone.utc).isoformat()
    features: list[dict] = []

    # Latitude band polygons
    for name, lat_min, lat_max in LAT_BANDS:
        mid_lat = (lat_min + lat_max) / 2.0
        risk = compute_risk(mid_lat, 0.0, kp)
        a = risk["assessment"]
        fill = _RISK_FILL.get(a["risk_level"], "#10B981")

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "feature_type": "zone",
                    "name": name,
                    "zone": risk["zone"],
                    "risk_level": a["risk_level"],
                    "risk_score": a["risk_score"],
                    "gps_error_m": a["gps_error_m"],
                    "hf_absorption_db": a["hf_absorption_db"],
                    "hf_blackout_prob": a["hf_blackout_probability"],
                    "satcom_fade_db": a["satcom_fade_db"],
                    "s4_index": a["s4_index"],
                    "pca_active": a["pca_active"],
                    "recommendation": a["recommendation"],
                    # GeoJSON styling hints (Mapbox / Leaflet)
                    "fill": fill,
                    "fill-opacity": 0.12,
                    "stroke": fill,
                    "stroke-opacity": 0.5,
                    "stroke-width": 1,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-180, lat_min],
                            [-180, lat_max],
                            [180, lat_max],
                            [180, lat_min],
                            [-180, lat_min],
                        ]
                    ],
                },
            }
        )

    # Installation point features
    for base in BASES:
        risk = compute_risk(base["lat"], base["lon"], kp)
        a = risk["assessment"]
        fill = _RISK_FILL.get(a["risk_level"], "#10B981")

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "feature_type": "installation",
                    "name": base["name"],
                    "zone": risk["zone"],
                    "kp_current": risk["kp_current"],
                    "bz_current_nt": risk["bz_current_nt"],
                    "risk_level": a["risk_level"],
                    "risk_score": a["risk_score"],
                    "gps_error_m": a["gps_error_m"],
                    "gps_error_range": a["gps_error_range"],
                    "vtec_estimate_tecu": a["vtec_estimate_tecu"],
                    "hf_absorption_db": a["hf_absorption_db"],
                    "hf_blackout_probability": a["hf_blackout_probability"],
                    "pca_active": a["pca_active"],
                    "satcom_fade_db": a["satcom_fade_db"],
                    "satcom_outage_probability": a["satcom_outage_probability"],
                    "radar_range_bias_lband_m": a["radar_range_bias_lband_m"],
                    "s4_index": a["s4_index"],
                    "watch_notes": a["watch_notes"],
                    "recommendation": a["recommendation"],
                    # GeoJSON styling
                    "marker-color": fill,
                    "marker-size": "medium",
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [base["lon"], base["lat"]],
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "name": "IonShield Ionospheric Risk",
        "generated": now,
        "kp_current": kp,
        "features": features,
    }
