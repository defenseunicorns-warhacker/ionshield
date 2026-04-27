"""
Phase 3a — ATAK readiness pack.

Generates the artifacts an ATAK / WinTAK / iTAK operator needs to consume
IonShield data in the field:

  - `network_link_xml(host)`: a KML <NetworkLink> document the operator
    drops into ATAK once. ATAK then auto-refreshes our /overlay/risk.kml
    every N seconds. No app re-install when our data changes — the link
    is durable.
  - `offline_pack_kmz()`: a single self-contained KMZ snapshot of current
    risk + 24h Kp forecast that survives DDIL (Disconnected, Degraded,
    Intermittent, Limited) operation. Operator caches it pre-mission and
    falls back to it if comms drop.

Both consume the same KML generator already used by /overlay/risk.kml so
the visual style is identical to the live network link.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

from app.outputs.kml import generate_kml

logger = logging.getLogger(__name__)


def network_link_xml(*, base_url: str, refresh_seconds: int = 300, view_refresh: bool = True) -> str:
    """
    Build a KML NetworkLink document an ATAK operator imports once.

    `base_url` should be the public origin (e.g. https://ionshield.app).
    `refresh_seconds` controls how often ATAK re-pulls the overlay.
    `view_refresh` enables onStop refresh — refetches when the operator
    stops panning, conserves bandwidth on the move.
    """
    base = base_url.rstrip("/")
    refresh_mode = "onInterval"
    view_block = ""
    if view_refresh:
        view_block = """
      <viewRefreshMode>onStop</viewRefreshMode>
      <viewRefreshTime>4</viewRefreshTime>
      <viewBoundScale>1.0</viewBoundScale>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>IonShield — Live Risk Overlay</name>
    <description><![CDATA[
      Real-time space-weather risk zones for GPS, HF radio, and SATCOM.
      Auto-refreshes every {refresh_seconds}s. Source: IonShield ({base}).
    ]]></description>
    <NetworkLink>
      <name>IonShield Risk Zones</name>
      <visibility>1</visibility>
      <open>1</open>
      <refreshVisibility>0</refreshVisibility>
      <flyToView>0</flyToView>
      <Link>
        <href>{base}/overlay/risk.kml</href>
        <refreshMode>{refresh_mode}</refreshMode>
        <refreshInterval>{refresh_seconds}</refreshInterval>{view_block}
      </Link>
    </NetworkLink>
  </Document>
</kml>
"""


def offline_pack_kmz(forecast: dict[str, Any] | None = None) -> bytes:
    """
    Build a self-contained KMZ snapshot for DDIL operation.

    Bundles:
      - doc.kml           — current risk overlay (same as /overlay/risk.kml)
      - forecast.kml      — 24h Kp forecast track (if provided)
      - README.txt        — operator-facing notes (timestamp, refresh hint)

    Returns the raw zip bytes. ATAK opens .kmz natively.
    """
    doc_kml = generate_kml()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", doc_kml)
        if forecast and forecast.get("entries"):
            zf.writestr("forecast.kml", _forecast_kml(forecast))
        zf.writestr("README.txt", _readme_text(forecast))
    return buf.getvalue()


def _forecast_kml(forecast: dict[str, Any]) -> str:
    """Build a small KML overlay marking each forecast horizon as a
    placemark over the magnetic pole, color-coded by severity."""
    severity_color = {
        "G0": "ff10b981",  # green
        "G1": "ff22c55e",
        "G2": "fff59e0b",  # amber
        "G3": "fff97316",
        "G4": "ffef4444",  # red
        "G5": "ffdc2626",  # deep red
    }
    placemarks: list[str] = []
    for i, entry in enumerate(forecast.get("entries", [])):
        sev = entry.get("severity", "G0")
        color = severity_color.get(sev, "ff10b981")
        kp = entry.get("kp_predicted", 0.0)
        h = entry.get("horizon_h", 0)
        valid = entry.get("valid_at", "")
        # Place markers near the geomagnetic pole for visibility
        lon = -110 + i * 6
        lat = 80
        placemarks.append(
            f"""    <Placemark>
      <name>+{h}h: Kp {kp:.1f} ({sev})</name>
      <description>Valid {valid}</description>
      <Style><IconStyle><color>{color}</color><scale>1.4</scale></IconStyle></Style>
      <Point><coordinates>{lon},{lat},0</coordinates></Point>
    </Placemark>"""
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>IonShield 24h Kp Forecast</name>
{chr(10).join(placemarks)}
  </Document>
</kml>
"""


def _readme_text(forecast: dict[str, Any] | None) -> str:
    from datetime import datetime, timezone

    lines = [
        "IonShield offline pack",
        "=" * 24,
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Contents:",
        "  doc.kml       Current risk overlay (regions colored by HF/GPS severity)",
    ]
    if forecast:
        lines.append("  forecast.kml  24h Kp forecast (placemarks at +1/+3/+6/+12/+24h)")
    lines.extend(
        [
            "",
            "Use:",
            "  Drag this .kmz into ATAK to load both layers. Use as a fallback",
            "  when network link to IonShield is unavailable. Refresh by",
            "  pulling a new pack from /atak/offline-pack.kmz when comms",
            "  return.",
            "",
            "WARNING: This is a snapshot. For live data, configure the",
            "network link from /atak/network-link.kml.",
        ]
    )
    return "\n".join(lines) + "\n"
