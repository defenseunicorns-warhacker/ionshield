"""
NANU — GPS Notice Advisory to Navstar Users (PROTOTYPE).

NANUs are GPS satellite scheduled/unscheduled outage advisories. They drive
PNT availability: a degraded constellation hurts navigation confidence,
RTK/autosteer readiness, and any GPS-dependent mission.

LIVE STATUS — HONEST: there is no public machine-readable NANU API. The
authoritative source (US Coast Guard NAVCEN) publishes NANUs as HTML/text,
and CelesTrak GPS-ops status (an optional future feed) is the realistic
live substitute. So this module is a **prototype**: it exposes a clean,
normalized NANU model and a demo fixture for WarHacker, attempts a
configurable JSON endpoint if one is provided (NANU_URL), and otherwise
reports status "unavailable" — never a fake live claim.

Follows the existing feed pattern: module _cache, async fetch(),
cache_snapshot(); registered as a DataSource; persisted by state_cache.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_cache: dict = {
    "advisories": [],  # [{svn, prn, type, summary, start, end}]
    "source": None,  # "NANU feed" | "DEMO" | None
    "last_fetch": None,
    "fetch_status": {},  # {"nanu": "ok"|"unavailable"|"timeout"|"error"}
}


def _normalize(obj: dict, idx: int) -> dict:
    return {
        "nanu": obj.get("nanu") or obj.get("id") or f"NANU-{idx+1}",
        "svn": obj.get("svn"),
        "prn": obj.get("prn"),
        "type": (obj.get("type") or obj.get("category") or "OUTAGE").upper(),
        "summary": obj.get("summary") or obj.get("text") or "",
        "start": obj.get("start") or obj.get("start_time"),
        "end": obj.get("end") or obj.get("end_time"),
    }


async def fetch_nanu(timeout: float = 10.0) -> None:
    """Attempt a configured NANU JSON endpoint. No fake live data.

    With no NANU_URL configured (the default), this reports "unavailable" —
    the honest state, since there is no public NANU API. A configured
    endpoint returning a JSON list of advisories is parsed and source-labeled.
    """
    url = (settings.nanu_url or "").strip()
    if not url:
        _cache["fetch_status"]["nanu"] = "unavailable"
        return
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        rows = data.get("advisories", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("Unexpected NANU payload shape")
        _cache["advisories"] = [_normalize(o, i) for i, o in enumerate(rows) if isinstance(o, dict)]
        _cache["source"] = "NANU feed"
        _cache["last_fetch"] = datetime.now(timezone.utc).isoformat()
        _cache["fetch_status"]["nanu"] = "ok"
        logger.debug("NANU: %d advisories", len(_cache["advisories"]))
    except httpx.TimeoutException:
        _cache["fetch_status"]["nanu"] = "timeout"
    except httpx.HTTPStatusError as exc:
        _cache["fetch_status"]["nanu"] = f"http_{exc.response.status_code}"
    except Exception as exc:
        logger.warning("NANU fetch error: %s", exc)
        _cache["fetch_status"]["nanu"] = "error"


# ── Accessors ─────────────────────────────────────────────────────────────────


def active_advisories() -> list[dict]:
    return list(_cache.get("advisories") or [])


def has_active_outage() -> bool:
    return any(a.get("type") in ("OUTAGE", "UNUSABLE", "FCSTDV", "FCSTUUFN") for a in active_advisories())


def available() -> bool:
    return _cache.get("source") is not None and bool(_cache.get("advisories") is not None and _cache.get("source"))


def cache_snapshot() -> dict:
    return {
        "last_fetch": _cache["last_fetch"],
        "fetch_status": dict(_cache["fetch_status"]),
        "source": _cache["source"],
        "advisory_count": len(active_advisories()),
        "has_active_outage": has_active_outage(),
        "available": _cache.get("source") is not None,
    }


# ── Demo injection (WarHacker fixture — clearly labeled DEMO) ─────────────────


def set_demo_outage() -> None:
    """Populate a synthetic, clearly-labeled DEMO NANU: an unscheduled
    outage on one SV plus a scheduled maintenance. NOT live data."""
    now = datetime.now(timezone.utc)
    _cache.update(
        {
            "advisories": [
                {
                    "nanu": "2026045",
                    "svn": "SVN-62",
                    "prn": "PRN-25",
                    "type": "UNUSABLE",
                    "summary": "Unscheduled outage — satellite set unusable until further notice",
                    "start": now.isoformat(),
                    "end": None,
                },
                {
                    "nanu": "2026046",
                    "svn": "SVN-50",
                    "prn": "PRN-05",
                    "type": "FCSTMX",
                    "summary": "Scheduled maintenance — forecast outage window",
                    "start": now.isoformat(),
                    "end": None,
                },
            ],
            "source": "DEMO",
            "last_fetch": now.isoformat(),
        }
    )
    _cache["fetch_status"]["nanu"] = "demo"


def clear() -> None:
    _cache.update({"advisories": [], "source": None})
    _cache["fetch_status"].pop("nanu", None)
