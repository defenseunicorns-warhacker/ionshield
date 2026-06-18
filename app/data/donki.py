"""
DONKI — NASA Space Weather Database Of Notifications, Knowledge, Information.

DONKI is the authoritative event log behind the conditions IonShield already
measures: solar flares (FLR), coronal mass ejections (CME), solar energetic
particle events (SEP), and geomagnetic storms (GST). Where the SWPC feeds tell
us *what the ionosphere is doing right now*, DONKI tells us *why* — the driving
event, when it occurred, and (for CMEs) the forecast arrival. That turns a bare
risk number into an operator-facing cause-of-risk explanation and timeline.

Source (real, machine-readable JSON):
    https://api.nasa.gov/DONKI/notifications?type=all&api_key=<KEY>

The default DEMO_KEY works but is rate-limited; set NASA_API_KEY for a real
quota. Goes through the same feed pattern as ustec/drap: module _cache, async
fetch(), cache_snapshot(); registered as a DataSource; persisted by state_cache.

Honesty contract: a real fetch is source-labeled "NASA DONKI"; a demo injection
is "DEMO"; absent either, status is "unavailable" and the mission layer simply
omits the event-context block (it never invents an event).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DONKI_BASE = "https://api.nasa.gov/DONKI/notifications"
LOOKBACK_DAYS = 3  # recent space-weather event window

# DONKI messageType → operator-facing label
_TYPE_LABEL = {
    "FLR": "Solar flare",
    "CME": "Coronal mass ejection",
    "SEP": "Solar energetic particle event",
    "GST": "Geomagnetic storm",
    "IPS": "Interplanetary shock",
    "RBE": "Radiation belt enhancement",
    "MPC": "Magnetopause crossing",
    "report": "SWPC report",
}

_cache: dict = {
    "events": [],  # [{type, label, issued, summary}]
    "source": None,  # "NASA DONKI" | "DEMO" | None
    "last_fetch": None,
    "fetch_status": {},  # {"donki": "ok"|"timeout"|"http_NNN"|"error"}
}


# ── Parser ────────────────────────────────────────────────────────────────────


def _summarize(body: str) -> str:
    """First meaningful line of a DONKI message body, trimmed for display."""
    if not body:
        return ""
    for line in body.splitlines():
        s = line.strip()
        if s and not s.lower().startswith(("message type", "##", "disclaimer")):
            return s[:240]
    return body.strip()[:240]


def _normalize(obj: dict) -> dict:
    mtype = (obj.get("messageType") or "report").strip()
    return {
        "type": mtype,
        "label": _TYPE_LABEL.get(mtype, mtype),
        "issued": obj.get("messageIssueTime"),
        "summary": _summarize(obj.get("messageBody") or ""),
    }


# ── Fetcher ─────────────────────────────────────────────────────────────────


async def fetch_donki(timeout: float = 12.0) -> None:
    """Fetch recent DONKI notifications. Fails cleanly (status only)."""
    now = datetime.now(timezone.utc)
    params = {
        "type": "all",
        "startDate": (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
        "endDate": now.strftime("%Y-%m-%d"),
        "api_key": settings.nasa_api_key or "DEMO_KEY",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(DONKI_BASE, params=params)
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, list):
            raise ValueError("Unexpected DONKI payload shape")
        events = [_normalize(o) for o in data if isinstance(o, dict)]
        # newest first, drop the boilerplate "all-clear" reports for signal
        events.sort(key=lambda e: e.get("issued") or "", reverse=True)
        _cache["events"] = events
        _cache["source"] = "NASA DONKI"
        _cache["last_fetch"] = now.isoformat()
        _cache["fetch_status"]["donki"] = "ok"
        logger.debug("DONKI: %d notifications", len(events))
    except httpx.TimeoutException:
        _cache["fetch_status"]["donki"] = "timeout"
    except httpx.HTTPStatusError as exc:
        _cache["fetch_status"]["donki"] = f"http_{exc.response.status_code}"
    except Exception as exc:
        logger.warning("DONKI fetch error: %s", exc)
        _cache["fetch_status"]["donki"] = "error"


# ── Accessors ─────────────────────────────────────────────────────────────────


def recent_events(limit: int = 8) -> list[dict]:
    return list(_cache.get("events") or [])[:limit]


def events_of_type(*types: str) -> list[dict]:
    want = {t.upper() for t in types}
    return [e for e in (_cache.get("events") or []) if e.get("type", "").upper() in want]


def has_significant_activity() -> bool:
    """True if any flare / CME / SEP / geomagnetic-storm notice is present."""
    return bool(events_of_type("FLR", "CME", "SEP", "GST"))


def drivers_summary() -> list[str]:
    """One-line, operator-facing 'cause of risk' lines for the live drivers."""
    out: list[str] = []
    for e in events_of_type("FLR", "CME", "SEP", "GST")[:4]:
        when = (e.get("issued") or "")[:16].replace("T", " ")
        # surface a flare class if the body mentions one (e.g. "X1.2", "M5")
        cls = ""
        m = re.search(r"\b([XMC]\d(?:\.\d)?)\b", e.get("summary", ""))
        if m:
            cls = f" {m.group(1)}"
        out.append(f"{e['label']}{cls} — {when} UTC".strip())
    return out


def available() -> bool:
    return _cache.get("source") is not None


def cache_snapshot() -> dict:
    return {
        "last_fetch": _cache["last_fetch"],
        "fetch_status": dict(_cache["fetch_status"]),
        "source": _cache["source"],
        "event_count": len(_cache.get("events") or []),
        "has_significant_activity": has_significant_activity(),
        "drivers": drivers_summary(),
        "available": available(),
    }


# ── Demo injection (WarHacker fixture — clearly labeled DEMO) ─────────────────


def set_demo_events() -> None:
    """A clearly-labeled DEMO event timeline: an X-class flare + an
    Earth-directed CME + a strong geomagnetic storm. NOT live data."""
    now = datetime.now(timezone.utc)
    _cache.update(
        {
            "events": [
                {
                    "type": "FLR",
                    "label": "Solar flare",
                    "issued": now.isoformat(),
                    "summary": "DEMO: X1.8 flare from AR1402, R3 radio blackout",
                },
                {
                    "type": "CME",
                    "label": "Coronal mass ejection",
                    "issued": (now - timedelta(hours=1)).isoformat(),
                    "summary": "DEMO: Earth-directed CME, estimated arrival +36h",
                },
                {
                    "type": "GST",
                    "label": "Geomagnetic storm",
                    "issued": (now - timedelta(hours=2)).isoformat(),
                    "summary": "DEMO: G3 (strong) geomagnetic storm in progress",
                },
            ],
            "source": "DEMO",
            "last_fetch": now.isoformat(),
        }
    )
    _cache["fetch_status"]["donki"] = "demo"


def clear() -> None:
    _cache.update({"events": [], "source": None})
    _cache["fetch_status"].pop("donki", None)
