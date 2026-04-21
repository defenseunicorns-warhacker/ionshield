"""
IonShield configurable location store.

Manages a list of named, monitored locations (installations, bases, assets).
The JSON file is loaded at startup and reloaded on every NOAA refresh so
operators can update the list without restarting the service.

Alert flow (debounced to suppress transient spikes):
  Enter alert: risk_level >= threshold for 2 consecutive assessments
  Clear alert: risk_level <  threshold for 3 consecutive assessments

With a 5-minute refresh interval the debounce gives ~10 min to enter and
~15 min to clear, which filters noise from single-3-hour-block Kp updates.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RISK_ORDER: dict[str, int] = {
    "NOMINAL": 0,
    "ELEVATED": 1,
    "DEGRADED": 2,
    "SEVERE": 3,
}

# In-memory stores.
# _locations is replaced wholesale on each load.
# _alert_state persists across reloads so in-flight alerts aren't lost on hot-reload.
_locations: list[dict] = []
_assessments: dict[str, dict] = {}  # id → compute_risk() output
_alert_state: dict[str, dict] = {}  # id → alert tracking dict


# ── Config loader ─────────────────────────────────────────────────────────────


def load_locations(path_str: str, default_threshold: str = "ELEVATED") -> None:
    """
    Load location config from JSON file.

    File schema (array of objects):
      id            — unique string key (used in API paths and CoT UIDs)
      name          — human-readable name
      lat           — latitude  (decimal degrees, −90 to 90)
      lon           — longitude (decimal degrees, −180 to 180)
      asset_type    — optional, default GPS_L1
      alert_threshold — optional, default from settings.alert_threshold
    """
    path = Path(path_str)
    if not path.exists():
        if _locations:
            logger.info("Locations file removed — monitoring disabled")
        _locations.clear()
        return

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("locations.json must be a JSON array")

        parsed: list[dict] = []
        for entry in raw:
            missing = [k for k in ("id", "name", "lat", "lon") if k not in entry]
            if missing:
                logger.warning("Skipping location entry missing %s: %s", missing, entry)
                continue
            parsed.append(
                {
                    "id": str(entry["id"]),
                    "name": str(entry["name"]),
                    "lat": float(entry["lat"]),
                    "lon": float(entry["lon"]),
                    "asset_type": str(entry.get("asset_type", "GPS_L1")),
                    "alert_threshold": str(
                        entry.get("alert_threshold", default_threshold)
                    ).upper(),
                }
            )

        _locations.clear()
        _locations.extend(parsed)
        logger.debug("Loaded %d locations from %s", len(_locations), path)

    except Exception as exc:
        logger.error("Failed to load locations from %s: %s", path, exc)


# ── Assessment ────────────────────────────────────────────────────────────────


def assess_all(kp: float) -> None:
    """Run the risk model for every configured location and update alert state."""
    from app.models.risk import compute_risk  # deferred to avoid circular import

    for loc in _locations:
        try:
            risk = compute_risk(
                loc["lat"], loc["lon"], kp, asset_type=loc["asset_type"]
            )
            _assessments[loc["id"]] = risk
            _tick_alert(loc, risk["assessment"]["risk_level"])
        except Exception as exc:
            logger.warning("Assessment failed for location '%s': %s", loc["id"], exc)


def _tick_alert(loc: dict, level: str) -> None:
    loc_id = loc["id"]
    threshold = loc["alert_threshold"]
    in_alert = RISK_ORDER.get(level, 0) >= RISK_ORDER.get(threshold, 1)

    state = _alert_state.setdefault(
        loc_id,
        {
            "active": False,
            "entered_at": None,
            "cleared_at": None,
            "risk_level": level,
            "hot": 0,  # consecutive assessments at-or-above threshold
            "cool": 0,  # consecutive assessments below threshold
        },
    )
    state["risk_level"] = level

    if in_alert:
        state["hot"] += 1
        state["cool"] = 0
        if not state["active"] and state["hot"] >= 2:
            state["active"] = True
            state["entered_at"] = datetime.now(timezone.utc).isoformat()
            state["cleared_at"] = None
            logger.warning(
                "ALERT ACTIVE — %s (%s) at %s (threshold %s)",
                loc["name"],
                loc_id,
                level,
                threshold,
            )
    else:
        state["cool"] += 1
        state["hot"] = 0
        if state["active"] and state["cool"] >= 3:
            state["active"] = False
            state["cleared_at"] = datetime.now(timezone.utc).isoformat()
            logger.info(
                "ALERT CLEARED — %s (%s) back to %s",
                loc["name"],
                loc_id,
                level,
            )


# ── Read accessors ────────────────────────────────────────────────────────────


def get_all() -> list[dict]:
    """All configured locations with their latest assessment and alert state."""
    result = []
    for loc in _locations:
        alert = _alert_state.get(loc["id"], {})
        result.append(
            {
                "id": loc["id"],
                "name": loc["name"],
                "lat": loc["lat"],
                "lon": loc["lon"],
                "asset_type": loc["asset_type"],
                "alert_threshold": loc["alert_threshold"],
                "assessment": _assessments.get(loc["id"]),
                "alert": {
                    "active": alert.get("active", False),
                    "entered_at": alert.get("entered_at"),
                    "cleared_at": alert.get("cleared_at"),
                    "risk_level": alert.get("risk_level", "NOMINAL"),
                },
            }
        )
    return result


def get_by_id(loc_id: str) -> Optional[dict]:
    for item in get_all():
        if item["id"] == loc_id:
            return item
    return None


def get_active_alerts() -> list[dict]:
    return [item for item in get_all() if item["alert"]["active"]]


def location_count() -> int:
    return len(_locations)
