"""
Cache-and-carry: persist the last-known feed state across restarts.

The disconnected-operations pattern (briefing-book Q6): sync once while
connected — at the FOB, before convoy departure, at the connected UDS node —
then carry the state into the air gap. Every successful fetch cycle writes
the full NOAA + ionosphere caches (raw feed payloads, fetch timestamps,
fetch status) to one JSON file. An OFFLINE_MODE boot rehydrates from that
file, so the platform serves real observations and the real NOAA 3-day Kp
forecast instead of conservative fallbacks.

Honesty contract:
  • The ORIGINAL fetch timestamp is preserved — data age is computed from
    when NOAA actually produced the data, never from the rehydration time.
  • `fetch_source` is set to "cached" after hydration so every consumer
    (status endpoint, data-quality scoring, ADVISORY labels) can tell
    carried state from live telemetry.
  • The 3-day Kp forecast inside the carried state remains genuinely valid
    for its forecast horizon — advisory_valid_until() exposes that bound.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.data import donki, drap, nanu, noaa, ovation, ustec

logger = logging.getLogger(__name__)

# v2 adds drap + nanu; v3 adds donki + ovation to the carried state.
# (WMM needs no carry — it is computed locally with zero connectivity.)
STATE_VERSION = 3

# Forecast horizon of the carried state: NOAA's Kp forecast product covers
# 3 days from issue. Past saved_at + 72h the advisory window is over.
ADVISORY_HORIZON_HOURS = 72

# Module flag: True when the current in-memory caches came from disk rather
# than a live fetch this process. Cleared by the next successful live fetch.
_hydrated_from: str | None = None  # ISO timestamp the carried state was saved


def _ovation_serializable() -> dict:
    """OVATION's grid is keyed by (lat, lon) tuples — not JSON-safe. Flatten
    the grid to [lat, lon, prob] rows for persistence; everything else copies."""
    out = {k: v for k, v in ovation._cache.items() if k != "grid"}
    grid = ovation._cache.get("grid") or {}
    out["grid_rows"] = [[la, lo, p] for (la, lo), p in grid.items()]
    return out


def _restore_ovation(saved: dict) -> None:
    """Inverse of _ovation_serializable: rebuild the tuple-keyed grid."""
    for k, v in saved.items():
        if k == "grid_rows":
            ovation._cache["grid"] = {(int(r[0]), int(r[1])): float(r[2]) for r in (v or [])}
        elif k in ovation._cache:
            ovation._cache[k] = v


def _path() -> Path | None:
    if not settings.state_cache_file:
        return None
    return Path(settings.state_cache_file)


def save_state() -> bool:
    """Persist the current NOAA + ionosphere caches. Atomic write.

    Called after each successful live fetch cycle. Returns True on write.
    """
    path = _path()
    if path is None:
        return False
    state = {
        "version": STATE_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "noaa": {k: v for k, v in noaa._cache.items()},
        "ionosphere": {k: v for k, v in ustec._cache.items()},
        "drap": {k: v for k, v in drap._cache.items()},
        "nanu": {k: v for k, v in nanu._cache.items()},
        "donki": {k: v for k, v in donki._cache.items()},
        "ovation": _ovation_serializable(),
    }
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent) or ".", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.warning("state_cache: save failed: %s", exc)
        return False


def load_state() -> dict | None:
    path = _path()
    if path is None or not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
        if state.get("version") != STATE_VERSION or "noaa" not in state:
            logger.warning("state_cache: unrecognized state file format — ignoring")
            return None
        return state
    except Exception as exc:
        logger.warning("state_cache: load failed: %s", exc)
        return None


def hydrate() -> bool:
    """Restore the in-memory feed caches from the persisted state.

    The original `last_fetch` timestamps are kept (honest data age);
    `fetch_source` is marked "cached". Returns True when state was applied.
    """
    global _hydrated_from
    state = load_state()
    if state is None:
        return False

    for module, key in ((noaa, "noaa"), (ustec, "ionosphere"), (drap, "drap"), (nanu, "nanu"), (donki, "donki")):
        saved = state.get(key) or {}
        for k, v in saved.items():
            if k in module._cache:
                module._cache[k] = v
    _restore_ovation(state.get("ovation") or {})
    noaa._cache["fetch_source"] = "cached"
    _hydrated_from = state.get("saved_at")
    logger.info(
        "state_cache: hydrated carried feed state saved at %s (advisory valid until %s)",
        _hydrated_from,
        advisory_valid_until(),
    )
    return True


def hydrated_from() -> str | None:
    """ISO timestamp the carried state was saved, or None if running live."""
    return _hydrated_from


def mark_live() -> None:
    """Called after a successful live fetch — carried state superseded."""
    global _hydrated_from
    _hydrated_from = None


def advisory_valid_until() -> str | None:
    """End of the carried forecast's validity (saved_at + 72 h), ISO."""
    if _hydrated_from is None:
        return None
    try:
        saved = datetime.fromisoformat(_hydrated_from)
    except ValueError:
        return None
    return (saved + timedelta(hours=ADVISORY_HORIZON_HOURS)).isoformat()


def advisory_note() -> str | None:
    """Operator-facing ADVISORY line, or None when running on live data."""
    if _hydrated_from is None:
        return None
    valid = advisory_valid_until()
    return (
        f"ADVISORY — operating on cached NOAA state synced {_hydrated_from}; "
        f"carried 3-day Kp forecast valid until {valid}. Sync when connectivity allows."
    )
