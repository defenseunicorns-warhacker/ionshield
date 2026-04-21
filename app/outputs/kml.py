"""
ATAK-compatible KML generator.

Produces a KML Document containing:
  1. Ionospheric risk zones (7 latitude bands, global longitude span)
  2. Military installation placemarks with full per-location risk assessments

KML colour format: AABBGGRR (alpha, blue, green, red).
Zone colours transition from green (NOMINAL) through amber (ELEVATED/DEGRADED)
to red (SEVERE) based on per-zone risk level at the time of generation.
"""

from datetime import datetime, timezone

from app.data.noaa import get_kp
from app.models.risk import compute_risk, BASES

# Latitude band definitions: (display_name, lat_min, lat_max, zone_type)
LAT_BANDS: list[tuple[str, float, float, str]] = [
    ("Polar North (>70°N)",           70,  90, "polar"),
    ("Sub-Auroral North (55–70°N)",   55,  70, "sub-auroral"),
    ("Mid-Latitude North (25–55°N)",  25,  55, "mid-latitude"),
    ("Equatorial (25°S–25°N)",       -25,  25, "equatorial"),
    ("Mid-Latitude South (25–55°S)", -55, -25, "mid-latitude"),
    ("Sub-Auroral South (55–70°S)",  -70, -55, "sub-auroral"),
    ("Polar South (>70°S)",          -90, -70, "polar"),
]

# KML colours by risk level: AABBGGRR
_RISK_COLORS: dict[str, dict[str, str]] = {
    "NOMINAL":   {"poly": "1400b310", "line": "4400b310"},  # green, low alpha
    "ELEVATED":  {"poly": "1a009ef5", "line": "4d009ef5"},  # amber
    "DEGRADED":  {"poly": "260000ee", "line": "660000ee"},  # orange-red
    "SEVERE":    {"poly": "400000cc", "line": "990000cc"},  # deep red
}


def _kml_style(style_id: str, risk_level: str) -> str:
    c = _RISK_COLORS.get(risk_level, _RISK_COLORS["NOMINAL"])
    return (
        f'  <Style id="{style_id}">\n'
        f'    <PolyStyle><color>{c["poly"]}</color><outline>1</outline></PolyStyle>\n'
        f'    <LineStyle><color>{c["line"]}</color><width>1</width></LineStyle>\n'
        f'  </Style>\n'
    )


def generate_kml() -> str:
    """Build and return a complete ATAK-compatible KML document string."""
    kp  = get_kp()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Pre-compute zone risk assessments (use lon=0 for zonal average assessment)
    zone_risks: list[dict] = []
    for name, lat_min, lat_max, ztype in LAT_BANDS:
        mid_lat = (lat_min + lat_max) / 2.0
        risk = compute_risk(mid_lat, 0.0, kp)
        zone_risks.append({"name": name, "lat_min": lat_min, "lat_max": lat_max,
                            "ztype": ztype, "risk": risk})

    # Dynamic styles — one per zone, keyed by band index to allow different levels
    styles_kml = ""
    for i, zr in enumerate(zone_risks):
        rl = zr["risk"]["assessment"]["risk_level"]
        styles_kml += _kml_style(f"zone_{i}", rl)

    # Zone polygons
    zones_kml = ""
    for i, zr in enumerate(zone_risks):
        r  = zr["risk"]
        a  = r["assessment"]
        lat_min, lat_max = zr["lat_min"], zr["lat_max"]
        desc = (
            f"<b>IonShield Zone Assessment</b><br/>"
            f"Zone: {r['zone'].title()} | Kp: {kp}<br/><br/>"
            f"GPS Error: <b>{a['gps_error_m']} m</b> "
            f"(range {a['gps_error_range'][0]}–{a['gps_error_range'][1]} m)<br/>"
            f"HF Absorption: {a['hf_absorption_db']} dB"
            f"{'  ★ PCA ACTIVE' if a['pca_active'] else ''}<br/>"
            f"SATCOM Fade: {a['satcom_fade_db']} dB<br/>"
            f"S4 Scintillation: {a['s4_index']}<br/>"
            f"Risk Score: {a['risk_score']}/99<br/>"
            f"Risk Level: <b>{a['risk_level']}</b><br/><br/>"
            f"<i>{a['recommendation']}</i><br/><br/>"
            f"Updated: {now} | Source: NOAA SWPC + IonShield"
        )
        coords = (
            f"-180,{lat_min},0 -180,{lat_max},0 "
            f"180,{lat_max},0 180,{lat_min},0 -180,{lat_min},0"
        )
        zones_kml += (
            f'    <Placemark>\n'
            f'      <name>{zr["name"]} — {a["risk_level"]}</name>\n'
            f'      <description><![CDATA[{desc}]]></description>\n'
            f'      <styleUrl>#zone_{i}</styleUrl>\n'
            f'      <Polygon><outerBoundaryIs><LinearRing>\n'
            f'        <coordinates>{coords}</coordinates>\n'
            f'      </LinearRing></outerBoundaryIs></Polygon>\n'
            f'    </Placemark>\n'
        )

    # Installation placemarks
    bases_kml = ""
    for base in BASES:
        risk = compute_risk(base["lat"], base["lon"], kp)
        a    = risk["assessment"]
        watch = (" | ".join(a["watch_notes"])) if a["watch_notes"] else "None"
        desc = (
            f"<b>{base['name']}</b><br/>"
            f"Zone: {risk['zone'].title()} ({risk['zone_multiplier']}×)<br/>"
            f"Kp: {kp} | Bz: {risk['bz_current_nt']} nT<br/><br/>"
            f"GPS Error: <b>{a['gps_error_m']} m</b> "
            f"[{a['gps_error_range'][0]}–{a['gps_error_range'][1]} m]<br/>"
            f"VTEC: ~{a['vtec_estimate_tecu']} TECU<br/>"
            f"HF Absorption: {a['hf_absorption_db']} dB "
            f"(SID: {a['hf_sid_db']} | Storm: {a['hf_storm_db']} | PCA: {a['hf_pca_db']})<br/>"
            f"HF Blackout Probability: {int(a['hf_blackout_probability']*100)}%<br/>"
            f"SATCOM Fade: {a['satcom_fade_db']} dB<br/>"
            f"Radar Bias (L-band): {a['radar_range_bias_lband_m']} m<br/>"
            f"S4 Scintillation: {a['s4_index']}<br/>"
            f"Risk Score: <b>{a['risk_score']}/99 — {a['risk_level']}</b><br/><br/>"
            f"Watch: {watch}<br/><br/>"
            f"<i>{a['recommendation']}</i><br/><br/>"
            f"Updated: {now}"
        )
        bases_kml += (
            f'    <Placemark>\n'
            f'      <name>{base["name"]} — {a["risk_level"]}</name>\n'
            f'      <description><![CDATA[{desc}]]></description>\n'
            f'      <Point><coordinates>{base["lon"]},{base["lat"]},0</coordinates></Point>\n'
            f'    </Placemark>\n'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        '<Document>\n'
        f'  <name>IonShield Ionospheric Risk</name>\n'
        f'  <description>Real-time ionospheric risk zones and installation assessments. '
        f'Kp: {kp}. Generated: {now}. Source: NOAA SWPC + IonShield v3.</description>\n'
        f'{styles_kml}'
        f'  <Folder>\n'
        f'    <name>Ionospheric Risk Zones</name>\n'
        f'{zones_kml}'
        f'  </Folder>\n'
        f'  <Folder>\n'
        f'    <name>Military Installations</name>\n'
        f'{bases_kml}'
        f'  </Folder>\n'
        '</Document>\n'
        '</kml>'
    )
