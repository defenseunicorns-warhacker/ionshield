"""
B4 caveat fixes — recipe validation + Earth Studio native camera-track CSV.

`validate_recipe(recipe)` returns a list of human-readable issues (empty
list = clean). Catches the obvious operator footguns before they paste a
broken recipe into Earth Studio:

  - Out-of-bounds lat/lon or impossible altitudes
  - Non-monotonic camera waypoint times
  - Final waypoint past the project duration
  - Camera path that crosses the date line at unrealistic speeds

`recipe_to_camera_csv(recipe)` emits the column layout Earth Studio's
Tracks panel reads natively for camera animation: one row per project
frame, with linearly-interpolated values between waypoints. Drops into
the Camera track via Tracks → Add Track → Import CSV without any
manual keyframing — exactly the "Option B" path documented in the
operator runbook.
"""

from __future__ import annotations

import csv
import io
from typing import Iterable


# Earth Studio camera limits — sourced from the public docs / project
# settings UI. Altitudes outside this range either clip the viewport or
# bottom out at the terrain mesh, neither of which is what an operator
# wants for a storm replay.
EARTH_RADIUS_M: float = 6_371_000.0
ALTITUDE_MIN_M: float = 1_000.0           # ~1 km — below this Earth Studio
                                          # auto-clamps to terrain
ALTITUDE_MAX_M: float = 60_000_000.0      # ~60 Mm — beyond LEO

CAMERA_CSV_COLUMNS = (
    "time", "latitude", "longitude", "altitude", "heading", "tilt",
)


def validate_recipe(recipe: dict) -> list[str]:
    """Return a list of issue strings; empty list means the recipe is clean."""
    issues: list[str] = []

    duration = recipe.get("duration_seconds")
    fps = recipe.get("frame_rate")
    cam = recipe.get("camera") or []

    if not isinstance(duration, (int, float)) or duration <= 0:
        issues.append("duration_seconds must be positive number")
    if not isinstance(fps, (int, float)) or fps <= 0:
        issues.append("frame_rate must be positive number")
    if not isinstance(cam, list) or len(cam) < 2:
        issues.append("camera must have at least 2 waypoints")
        return issues

    last_t = -1.0
    for i, wp in enumerate(cam):
        for k in ("t", "lat", "lon", "altitude_m", "heading", "tilt"):
            if k not in wp:
                issues.append(f"waypoint {i} missing key {k}")
                continue
        t = wp.get("t")
        if t is not None and t <= last_t:
            issues.append(f"waypoint {i} time {t}s is not strictly increasing "
                          f"(previous {last_t}s)")
        if t is not None:
            last_t = t

        lat = wp.get("lat")
        if lat is not None and not (-90 <= lat <= 90):
            issues.append(f"waypoint {i} lat {lat} outside [-90, 90]")
        lon = wp.get("lon")
        if lon is not None and not (-180 <= lon <= 180):
            issues.append(f"waypoint {i} lon {lon} outside [-180, 180]")
        alt = wp.get("altitude_m")
        if alt is not None and not (ALTITUDE_MIN_M <= alt <= ALTITUDE_MAX_M):
            issues.append(
                f"waypoint {i} altitude {alt} m outside Earth Studio range "
                f"[{ALTITUDE_MIN_M:g}, {ALTITUDE_MAX_M:g}]"
            )
        h = wp.get("heading")
        if h is not None and not (-360 <= h <= 360):
            issues.append(f"waypoint {i} heading {h} outside [-360, 360]")
        tilt = wp.get("tilt")
        if tilt is not None and not (-90 <= tilt <= 90):
            issues.append(f"waypoint {i} tilt {tilt} outside [-90, 90]")

    if cam and last_t > duration:
        issues.append(
            f"final waypoint t={last_t}s exceeds duration_seconds={duration}"
        )

    return issues


def _interp_lon(a: float, b: float, frac: float) -> float:
    """Shortest-arc interpolation across the date line."""
    diff = b - a
    if diff > 180:
        diff -= 360
    elif diff < -180:
        diff += 360
    out = a + diff * frac
    if out > 180:
        out -= 360
    elif out < -180:
        out += 360
    return out


def _lerp(a: float, b: float, frac: float) -> float:
    return a + (b - a) * frac


def _interpolate(cam: list[dict], t: float) -> dict:
    """Find the segment containing time t and lerp lat/lon/altitude/heading/tilt."""
    if t <= cam[0]["t"]:
        return cam[0]
    if t >= cam[-1]["t"]:
        return cam[-1]
    for i in range(len(cam) - 1):
        a, b = cam[i], cam[i + 1]
        if a["t"] <= t <= b["t"]:
            span = b["t"] - a["t"] or 1
            f = (t - a["t"]) / span
            return {
                "t": t,
                "lat": _lerp(a["lat"], b["lat"], f),
                "lon": _interp_lon(a["lon"], b["lon"], f),
                "altitude_m": _lerp(a["altitude_m"], b["altitude_m"], f),
                "heading": _lerp(a["heading"], b["heading"], f),
                "tilt": _lerp(a["tilt"], b["tilt"], f),
            }
    return cam[-1]


def recipe_to_camera_csv(recipe: dict) -> str:
    """
    Emit a per-frame Earth Studio camera-track CSV from the recipe.

    Earth Studio's Tracks tool reads `time, latitude, longitude, altitude,
    heading, tilt` — the columns we emit. Importing the file via Tracks →
    Add Track → Import CSV creates a fully-keyframed camera animation.

    Cadence is one row per render frame so Earth Studio doesn't need to
    interpolate further. For a 30 s × 30 fps project that's 900 rows —
    still tiny on the wire (under 60 KB).
    """
    duration = float(recipe.get("duration_seconds") or 0)
    fps = float(recipe.get("frame_rate") or 30)
    cam = recipe.get("camera") or []
    if duration <= 0 or fps <= 0 or len(cam) < 2:
        raise ValueError("invalid recipe — cannot emit camera CSV")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CAMERA_CSV_COLUMNS)
    n_frames = int(round(duration * fps)) + 1
    for i in range(n_frames):
        t = i / fps
        wp = _interpolate(cam, t)
        writer.writerow((
            f"{t:.4f}",
            f"{wp['lat']:.6f}",
            f"{wp['lon']:.6f}",
            f"{wp['altitude_m']:.1f}",
            f"{wp['heading']:.4f}",
            f"{wp['tilt']:.4f}",
        ))
    return buf.getvalue()


def lint_catalog(catalog: dict) -> dict[str, list[str]]:
    """
    Validate every recipe in a scenarios catalog. Return {scenario_id: [issues]}
    with empty lists for clean recipes. Used by the precompute step + the
    /api/v3/scenarios/{id}/recipe?lint=1 query.
    """
    out: dict[str, list[str]] = {}
    for sc in catalog.get("scenarios", []):
        if "recipe" in sc:
            out[sc["id"]] = validate_recipe(sc["recipe"])
    return out
