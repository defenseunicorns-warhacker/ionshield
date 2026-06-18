"""
Live mission watch — keep an assessed mission running and react to live
space weather.

After a mission is assessed, the operator can register it as a *watch*. The
background feed-refresh loop re-evaluates every active watch each cycle against
the latest feeds; when the picture materially changes (mission verdict
escalates, a consequence appears, or a route segment degrades in the forecast
grid), the watch's version bumps and a human-readable change note is recorded.
Subscribers (the dashboard, via SSE) poll the version and render updates live.

Server-side, in-memory, single-replica — matches the rest of the platform. The
assessment function is injected (assessor) so this module stays decoupled from
the API layer. Disabled in OFFLINE_MODE: there are no live feeds to react to.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_WATCHES = 50  # backstop against unbounded growth (single-replica demo)

# watch_id -> {req, assessment, version, created_at, updated_at, change, label}
_watches: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signature(a: dict) -> tuple:
    """Compact signature of the mission-relevant state for change detection.

    Covers the verdict, the feed-driven consequences, and each forecast
    window's worst risk — i.e. exactly what an operator would notice change.
    """
    grid = a.get("segment_time_grid") or []
    return (
        a.get("mission_risk_level"),
        tuple(sorted((c.get("fn"), c.get("risk")) for c in (a.get("feed_consequences") or []))),
        tuple((w.get("time"), w.get("overall_risk")) for w in grid),
    )


_RISK_RANK = {"CLEAR": 0, "CAUTION": 1, "HIGH_RISK": 2, "DELAY": 3}


def _diff(prev: dict, cur: dict) -> Optional[list[str]]:
    """Human-readable summary of what changed between two assessments."""
    changes: list[str] = []
    pv, cv = prev.get("mission_risk_level"), cur.get("mission_risk_level")
    if pv != cv:
        arrow = "escalated" if _RISK_RANK.get(cv, 0) > _RISK_RANK.get(pv, 0) else "eased"
        changes.append(f"Mission risk {arrow}: {pv} → {cv}")

    # New feed consequences
    def cons(a):
        return {(c.get("fn"), c.get("risk")) for c in (a.get("feed_consequences") or [])}

    for fn, risk in sorted(cons(cur) - cons(prev)):
        changes.append(f"New consequence: {fn} ({risk})")

    # Forecast-grid segments that worsened
    def worst_by_time(a):
        return {w.get("time"): w.get("overall_risk") for w in (a.get("segment_time_grid") or [])}

    pg, cg = worst_by_time(prev), worst_by_time(cur)
    worsened = [t for t in cg if t in pg and _wp_rank(cg[t]) > _wp_rank(pg[t])]
    if worsened:
        changes.append(f"{len(worsened)} forecast window(s) degraded (next at {min(worsened)[:16]}Z)")
    return changes or None


_WP_RANK = {"NOMINAL": 0, "ELEVATED": 1, "DEGRADED": 2, "SEVERE": 3}


def _wp_rank(level: Optional[str]) -> int:
    return _WP_RANK.get(level or "NOMINAL", 0)


def register(req: Any, assessor: Callable[[Any], dict], label: str = "") -> tuple[str, dict]:
    """Assess the mission now and store it as an active watch. Returns
    (watch_id, initial_assessment)."""
    if len(_watches) >= MAX_WATCHES:
        # Evict the oldest to stay bounded.
        oldest = min(_watches, key=lambda k: _watches[k]["created_at"])
        _watches.pop(oldest, None)
    assessment = assessor(req)
    wid = uuid.uuid4().hex[:12]
    _watches[wid] = {
        "req": req,
        "assessment": assessment,
        "version": 1,
        "created_at": _now(),
        "updated_at": _now(),
        "change": None,
        "label": label or (getattr(req, "callsign", "") or "mission"),
    }
    logger.info("mission_watch: registered %s (%s); %d active", wid, _watches[wid]["label"], len(_watches))
    return wid, assessment


def get(watch_id: str) -> Optional[dict]:
    return _watches.get(watch_id)


def delete(watch_id: str) -> bool:
    existed = _watches.pop(watch_id, None) is not None
    if existed:
        logger.info("mission_watch: stopped %s; %d active", watch_id, len(_watches))
    return existed


def count() -> int:
    return len(_watches)


def reassess_all(assessor: Callable[[Any], dict]) -> list[str]:
    """Re-evaluate every active watch. Bump version + record the change note
    when the picture materially changed. Returns the list of changed ids.
    Best-effort: a single watch's error never aborts the batch."""
    changed: list[str] = []
    for wid, w in list(_watches.items()):
        try:
            new = assessor(w["req"])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("mission_watch: reassess %s failed: %s", wid, exc)
            continue
        if _signature(new) != _signature(w["assessment"]):
            w["change"] = _diff(w["assessment"], new)
            w["version"] += 1
            w["assessment"] = new
            w["updated_at"] = _now()
            changed.append(wid)
            logger.info("mission_watch: %s changed (v%d): %s", wid, w["version"], w["change"])
        else:
            # No material change — still refresh the stored data + timestamp.
            w["assessment"] = new
            w["updated_at"] = _now()
    return changed
