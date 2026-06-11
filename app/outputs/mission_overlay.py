"""
Time-windowed mission risk overlay for ATAK — KML with TimeSpan zones.

WarHacker P0-4. Serves the briefing-book demo moment: a color-coded risk
zone over the operator's AO that changes with time — green during the quiet
window, red during the storm window — loadable in ATAK/WinTAK as a plain
HTTP KML layer (no plugin, no SDK, no signing keys; ATAK's time slider
drives the TimeSpan visibility).

Two data paths, both real:
  • LIVE — windows come from NOAA's 3-day Kp forecast product
    (noaa-planetary-k-index-forecast.json), each 3-hour bin colored by the
    same QUIET/MODERATE/SEVERE thresholds the equipment rule library uses.
  • REPLAY — windows come from a ReplayScenario's recorded Kp timeline
    (GFZ definitive values), mapped hour-for-hour onto the chosen demo day
    and labeled REPLAY in the document description.

KML colors are aabbggrr hex (alpha first, then blue-green-red).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from app.models.equipment import WX_MODERATE, WX_QUIET, WX_SEVERE, classify_weather_state
from app.models.replay_scenarios import ReplayScenario

# Zone fill colors by weather state (aabbggrr), ~40% alpha.
_STATE_COLOR = {
    WX_QUIET: "6681b910",  # green  #10b981
    WX_MODERATE: "660b9ef5",  # amber  #f59e0b
    WX_SEVERE: "664444ef",  # red    #ef4444
}
_STATE_LINE = {
    WX_QUIET: "ff81b910",
    WX_MODERATE: "ff0b9ef5",
    WX_SEVERE: "ff4444ef",
}

EARTH_RADIUS_KM = 6371.0


def _circle_coordinates(lat: float, lon: float, radius_km: float, points: int = 36) -> str:
    """KML coordinate ring (lon,lat,alt triplets) for a circle around the AO."""
    coords = []
    ang_dist = radius_km / EARTH_RADIUS_KM
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    for i in range(points + 1):  # +1 closes the ring
        bearing = math.radians(i * 360.0 / points)
        p_lat = math.asin(
            math.sin(lat_r) * math.cos(ang_dist) + math.cos(lat_r) * math.sin(ang_dist) * math.cos(bearing)
        )
        p_lon = lon_r + math.atan2(
            math.sin(bearing) * math.sin(ang_dist) * math.cos(lat_r),
            math.cos(ang_dist) - math.sin(lat_r) * math.sin(p_lat),
        )
        coords.append(f"{math.degrees(p_lon):.5f},{math.degrees(p_lat):.5f},0")
    return " ".join(coords)


def _zone_placemark(
    lat: float,
    lon: float,
    radius_km: float,
    start: datetime,
    end: datetime,
    kp: float,
    state: str,
    source_label: str,
) -> str:
    """One time-windowed risk zone."""
    name = f"{state} · Kp {kp:.1f} · {start.strftime('%H:%M')}–{end.strftime('%H:%M')}Z"
    return (
        "<Placemark>"
        f"<name>{escape(name)}</name>"
        f"<description>{escape(f'Kp {kp:.2f} ({source_label}). State: {state}.')}</description>"
        f"<TimeSpan><begin>{start.isoformat()}</begin><end>{end.isoformat()}</end></TimeSpan>"
        "<Style>"
        f"<LineStyle><color>{_STATE_LINE[state]}</color><width>2</width></LineStyle>"
        f"<PolyStyle><color>{_STATE_COLOR[state]}</color></PolyStyle>"
        "</Style>"
        "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
        f"{_circle_coordinates(lat, lon, radius_km)}"
        "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
        "</Placemark>"
    )


def _document(name: str, description: str, placemarks: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        "<Document>"
        f"<name>{escape(name)}</name>"
        f"<description>{escape(description)}</description>"
        f"{''.join(placemarks)}"
        "</Document>\n</kml>\n"
    )


def build_live_overlay_kml(
    lat: float,
    lon: float,
    radius_km: float,
    forecast_entries: list[dict],
    horizon_hours: int = 72,
) -> str:
    """Overlay from the real NOAA 3-day Kp forecast.

    forecast_entries is app.models.forecast.parse_kp_forecast() output:
    [{"time": datetime, "kp": float, "type": "observed"|"forecast", ...}].
    Each entry is the start of a 3-hour bin. Past bins are dropped (ATAK
    cares about the mission window ahead, not history).
    """
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=horizon_hours)
    placemarks = []
    for entry in forecast_entries:
        start = entry["time"]
        end = start + timedelta(hours=3)
        if end <= now or start >= horizon:
            continue
        state = classify_weather_state(entry["kp"])
        label = "NOAA SWPC 3-day Kp forecast" if entry["type"] == "forecast" else "NOAA SWPC observed Kp"
        placemarks.append(_zone_placemark(lat, lon, radius_km, start, end, entry["kp"], state, label))

    return _document(
        "IonShield mission risk overlay",
        f"Time-windowed space-weather risk zones for AO ({lat:.3f}, {lon:.3f}), "
        f"radius {radius_km:g} km. Source: NOAA SWPC planetary Kp forecast (live). "
        "Zone visibility follows the ATAK/Google Earth time slider.",
        placemarks,
    )


def build_replay_overlay_kml(
    lat: float,
    lon: float,
    radius_km: float,
    scenario: ReplayScenario,
    day_start: datetime | None = None,
) -> str:
    """Overlay from a replay scenario's recorded Kp timeline.

    The recorded sequence is mapped hour-for-hour onto `day_start`
    (default: today 00:00 UTC), so the demo shows the storm's real shape —
    e.g. Gannon's quiet morning then G5 evening — on the current day's
    time slider. Labeled REPLAY throughout.
    """
    if day_start is None:
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    placemarks = []
    for hour_offset, kp in scenario.kp_timeline:
        start = day_start + timedelta(hours=hour_offset)
        end = start + timedelta(hours=3)
        state = classify_weather_state(kp)
        placemarks.append(
            _zone_placemark(lat, lon, radius_km, start, end, kp, state, f"REPLAY {scenario.id} — recorded Kp")
        )

    return _document(
        f"IonShield mission risk overlay — REPLAY {scenario.id}",
        f"REPLAY of {scenario.title} ({scenario.occurred}) mapped onto "
        f"{day_start.date().isoformat()} for AO ({lat:.3f}, {lon:.3f}), radius {radius_km:g} km. "
        f"Recorded measurements, not live conditions. {scenario.citation}.",
        placemarks,
    )
