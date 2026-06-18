"""
NANU — GPS Notice Advisory to Navstar Users + live constellation status.

NANUs are GPS satellite scheduled/unscheduled outage advisories. They drive
PNT availability: a degraded constellation hurts navigation confidence,
RTK/autosteer readiness, and any GPS-dependent mission.

LIVE DATA — HONEST sourcing. IonShield gets real, live GPS-availability data
in three tiers, best first:

  1. NANU_URL — an enclave / .mil NANU mirror (true per-SV outage advisories).
     This is the right source for a deployed unit; configure it and you get
     authoritative NANU text.
  2. NAVCEN GPS almanac (default, public, authoritative) — the U.S. Coast
     Guard Navigation Center's current YUMA almanac. NAVCEN is the official
     U.S. authority for GPS status and NANUs. The almanac carries a per-PRN
     Health field (000 = healthy), so we derive the real operational
     constellation: how many SVs are healthy vs the ~31-SV nominal baseline,
     and exactly which PRNs are set unhealthy/unusable right now. Reachable
     from data-center egress (unlike CelesTrak, which IP-blocks cloud hosts).
     Source-labeled "NAVCEN GPS almanac".
  3. DEMO fixture for WarHacker (labeled DEMO).

Follows the existing feed pattern: module _cache, async fetch(),
cache_snapshot(); registered as a DataSource; persisted by state_cache.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Authoritative, machine-readable GPS constellation health (USCG NAVCEN YUMA).
NAVCEN_YUMA_ALMANAC = "https://www.navcen.uscg.gov/sites/default/files/gps/almanac/current_yuma.alm"
_FEED_UA = "IonShield/1.0 (space-weather mission assurance; +https://ionshield.io)"
NOMINAL_OPERATIONAL_SVS = 31  # USAF commits to >=31 operational; baseline

_cache: dict = {
    "advisories": [],  # [{svn, prn, type, summary, start, end}] (NANU_URL/DEMO)
    "constellation": None,  # {operational_count, nominal, prns, unhealthy} (NAVCEN)
    "source": None,  # "NANU feed" | "NAVCEN GPS almanac" | "DEMO" | None
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
    """Live GPS-availability ingest. NANU_URL mirror first, else NAVCEN."""
    url = (settings.nanu_url or "").strip()
    if url:
        await _fetch_nanu_mirror(url, timeout)
    else:
        await _fetch_navcen_almanac(timeout)


async def _fetch_nanu_mirror(url: str, timeout: float) -> None:
    """Enclave / .mil NANU mirror returning a JSON list of advisories."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        rows = data.get("advisories", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("Unexpected NANU payload shape")
        _cache["advisories"] = [_normalize(o, i) for i, o in enumerate(rows) if isinstance(o, dict)]
        _cache["constellation"] = None
        _cache["source"] = "NANU feed"
        _cache["last_fetch"] = datetime.now(timezone.utc).isoformat()
        _cache["fetch_status"]["nanu"] = "ok"
    except httpx.TimeoutException:
        _cache["fetch_status"]["nanu"] = "timeout"
    except httpx.HTTPStatusError as exc:
        _cache["fetch_status"]["nanu"] = f"http_{exc.response.status_code}"
    except Exception as exc:
        logger.warning("NANU mirror fetch error: %s", exc)
        _cache["fetch_status"]["nanu"] = "error"


def _parse_yuma_almanac(text: str) -> tuple[list[int], list[int]]:
    """Parse a YUMA almanac → (healthy_prns, unhealthy_prns). Health 000 = OK."""
    blocks = re.split(r"\*+\s*Week\s+\d+\s+almanac for PRN-?0*(\d+)\s*\*+", text)
    healthy: list[int] = []
    unhealthy: list[int] = []
    it = iter(blocks[1:])  # blocks[0] is the pre-amble before the first PRN
    for prn, body in zip(it, it):
        m = re.search(r"Health:\s*(\d+)", body)
        if m is None:
            continue
        (healthy if int(m.group(1)) == 0 else unhealthy).append(int(prn))
    return sorted(healthy), sorted(unhealthy)


async def _fetch_navcen_almanac(timeout: float) -> None:
    """NAVCEN YUMA almanac → live, authoritative constellation health."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": _FEED_UA}
        ) as client:
            r = await client.get(NAVCEN_YUMA_ALMANAC)
            r.raise_for_status()
            healthy, unhealthy = _parse_yuma_almanac(r.text)
        if not healthy and not unhealthy:
            raise ValueError("No PRN blocks found in NAVCEN almanac")
        _cache["constellation"] = {
            "operational_count": len(healthy),
            "total_tracked": len(healthy) + len(unhealthy),
            "nominal": NOMINAL_OPERATIONAL_SVS,
            "prns": healthy,
            "unhealthy": unhealthy,
        }
        _cache["advisories"] = []
        _cache["source"] = "NAVCEN GPS almanac"
        _cache["last_fetch"] = datetime.now(timezone.utc).isoformat()
        _cache["fetch_status"]["nanu"] = "ok"
        logger.debug("NANU/NAVCEN: %d healthy, %d unhealthy SVs", len(healthy), len(unhealthy))
    except httpx.TimeoutException:
        _cache["fetch_status"]["nanu"] = "timeout"
    except httpx.HTTPStatusError as exc:
        _cache["fetch_status"]["nanu"] = f"http_{exc.response.status_code}"
    except Exception as exc:
        logger.warning("NANU/NAVCEN fetch error: %s", exc)
        _cache["fetch_status"]["nanu"] = "error"


# ── Accessors ─────────────────────────────────────────────────────────────────


def active_advisories() -> list[dict]:
    return list(_cache.get("advisories") or [])


def constellation_status() -> dict | None:
    """Live operational GPS constellation health (NAVCEN almanac), or None."""
    c = _cache.get("constellation")
    if not c:
        return None
    op, nom = c["operational_count"], c["nominal"]
    unhealthy = list(c.get("unhealthy") or [])
    return {
        "operational_count": op,
        "total_tracked": c.get("total_tracked", op),
        "nominal": nom,
        "degraded": op < nom,
        "prns": list(c.get("prns") or []),
        "prn_count": len(c.get("prns") or []),
        "unhealthy": unhealthy,
    }


def has_active_outage() -> bool:
    """True if a NANU advisory marks an outage, the healthy constellation is
    below the nominal operational baseline, or an SV is set unhealthy."""
    if any(a.get("type") in ("OUTAGE", "UNUSABLE", "FCSTDV", "FCSTUUFN") for a in active_advisories()):
        return True
    c = constellation_status()
    return bool(c and (c["degraded"] or c["unhealthy"]))


def available() -> bool:
    return _cache.get("source") is not None


def cache_snapshot() -> dict:
    return {
        "last_fetch": _cache["last_fetch"],
        "fetch_status": dict(_cache["fetch_status"]),
        "source": _cache["source"],
        "advisory_count": len(active_advisories()),
        "constellation": constellation_status(),
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
    _cache.update({"advisories": [], "constellation": None, "source": None})
    _cache["fetch_status"].pop("nanu", None)
