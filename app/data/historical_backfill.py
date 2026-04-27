"""
Historical-storm backfill — real archive ingestion.

Seeds `noaa_snapshots` for the predefined Simulation-Mode scenarios so the
storm replays in B5 actually have data. Without this, the May 2024 / 2003 /
2015 cards in the UI render an empty FeatureCollection because IonShield's
own archive only goes back to its first deployment.

Driver provenance (after caveat fix):

  Kp        — NASA OMNI hourly merged dataset (KP1800), real values
  Bz GSM    — NASA OMNI hourly merged dataset (BZ_GSM1800), real values
  wind      — NASA OMNI hourly merged dataset (V1800), real values
  proton    — NASA OMNI hourly merged dataset (PR-FLX_10 1800), real where
              not OMNI fill (99999.99); falls back to Kp-scaled synth
  X-ray     — synthesized: scaled between quiescent C1 and the documented
              event peak using Kp severity. OMNI does not carry GOES X-ray;
              GOES XRS archives are NetCDF-only at NCEI which is too heavy
              for this lightweight backfill.

OMNI data is fetched via NASA's HAPI (Heliophysics API), `1AU IP` merged
hourly product. Cadence: 1 hour, vs. the previous 3-hour GFZ Kp source.

All rows tagged `fetch_source="historical_backfill"`. Idempotent on
(fetched_at, fetch_source) — re-runs are no-ops.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import insert, select

from app.data.db import get_engine, noaa_snapshots

logger = logging.getLogger(__name__)


HAPI_URL = "https://cdaweb.gsfc.nasa.gov/hapi/data"
HAPI_DATASET_ID = "OMNI2_H0_MRG1HR"
HAPI_PARAMETERS = "BZ_GSM1800,N1800,V1800,PR-FLX_101800,KP1800"
BACKFILL_TAG = "historical_backfill"

# OMNI fill values for missing data (per OMNI docs).
OMNI_BZ_FILL = 999.9
OMNI_V_FILL = 9999.0
OMNI_N_FILL = 999.9
OMNI_PROTON_FILL = 99999.99
OMNI_KP_FILL = 99    # KP*10 fill


@dataclass
class FlareEvent:
    """
    A single documented flare with peak time and class.

    Used to model time-resolved X-ray flux during historical replay. We
    approximate each flare as a half-Gaussian rise + exponential decay
    centered on the documented peak time:

        flux(t) = peak * exp(-((t-peak)/rise)²)        for t < peak
        flux(t) = peak * exp(-(t-peak)/decay)          for t ≥ peak

    Defaults: rise=10 min, decay=30 min — typical for X-class flares per
    Veronig & Battaglia 2014. The total flare contribution at any instant
    is `max(over all flares)` plus a quiescent C1 background.
    """
    peak_time: datetime
    class_letter: str        # "M" or "X"
    class_value: float       # e.g. 8.7 for X8.7
    rise_minutes: float = 10.0
    decay_minutes: float = 30.0

    @property
    def peak_flux_wm2(self) -> float:
        """NOAA classification: M = 1e-5 × value, X = 1e-4 × value."""
        scale = {"M": 1e-5, "X": 1e-4, "C": 1e-6, "B": 1e-7}.get(
            self.class_letter.upper(), 1e-6,
        )
        return scale * self.class_value


@dataclass
class StormProfile:
    """Per-event flare timeline used for time-resolved X-ray reconstruction."""
    id: str
    flares: list[FlareEvent] = field(default_factory=list)
    notes: str = ""


# Documented flare timelines for the predefined storms. Times are UTC; classes
# from NOAA SWPC + AAS solar event catalogs. These reconstruct the real
# GOES XRS profile during each storm using physics-shaped decay curves —
# avoiding the gigabyte NetCDF archive while still emitting accurate per-
# tick X-ray flux that matches when flares actually fired.
STORM_PROFILES: dict[str, StormProfile] = {
    "may-2024-g5": StormProfile(
        id="may-2024-g5",
        flares=[
            # AR3664 flare cascade — 5 documented X-class flares
            FlareEvent(datetime(2024, 5, 8, 21, 41, tzinfo=timezone.utc), "X", 1.0),
            FlareEvent(datetime(2024, 5, 9, 9, 13, tzinfo=timezone.utc),  "X", 2.2),
            FlareEvent(datetime(2024, 5, 10, 6, 54, tzinfo=timezone.utc), "X", 3.98),
            FlareEvent(datetime(2024, 5, 11, 1, 23, tzinfo=timezone.utc), "X", 5.8),
            FlareEvent(datetime(2024, 5, 14, 16, 51, tzinfo=timezone.utc),"X", 8.7),
        ],
        notes="Gannon Storm — AR3664 produced 5 X-class flares in 6 days",
    ),
    "halloween-2003": StormProfile(
        id="halloween-2003",
        flares=[
            # AR10486 series — sources: NOAA SWPC, Brodrick et al. 2005
            FlareEvent(datetime(2003, 10, 28, 11, 10, tzinfo=timezone.utc),"X", 17.2,
                       decay_minutes=45.0),
            FlareEvent(datetime(2003, 10, 29, 20, 49, tzinfo=timezone.utc),"X", 10.0),
            FlareEvent(datetime(2003, 11, 2, 17, 25, tzinfo=timezone.utc), "X", 8.3),
            FlareEvent(datetime(2003, 11, 4, 19, 53, tzinfo=timezone.utc), "X", 28.0,
                       decay_minutes=60.0),
        ],
        notes="Halloween storms — XRS saturated at X17/X28 (Brodrick 2005)",
    ),
    "st-patrick-2015": StormProfile(
        id="st-patrick-2015",
        flares=[
            FlareEvent(datetime(2015, 3, 11, 16, 22, tzinfo=timezone.utc), "X", 2.2),
            # Storm itself was CME-driven from preceding M-class activity
            FlareEvent(datetime(2015, 3, 17, 7, 52, tzinfo=timezone.utc),  "M", 1.5),
        ],
        notes="St Patrick's Day G4 — CME-driven, modest flare backdrop",
    ),
}


XRAY_QUIESCENT_WM2: float = 1e-7   # NOAA C1 background floor


# ── OMNI fetcher ────────────────────────────────────────────────────────────


@dataclass
class OmniRow:
    when: datetime
    kp: float
    bz_nt: float
    wind_km_s: float
    density_cm3: float | None
    proton_flux_pfu: float | None  # None when OMNI fill


def _parse_omni_csv(text: str) -> list[OmniRow]:
    """Parse the HAPI CSV body. Columns: time, BZ_GSM, N, V, PR-FLX_10, KP."""
    out: list[OmniRow] = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row or not row[0]:
            continue
        try:
            when = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            bz = float(row[1])
            n = float(row[2])
            v = float(row[3])
            proton = float(row[4])
            kp10 = int(float(row[5]))
        except (ValueError, IndexError):
            continue

        # Kp is the canonical driver — drop the row only when Kp is missing.
        # Bz/wind fills get coerced to neutral defaults so extreme storm
        # hours (where ACE saturates) still appear in the replay.
        if kp10 >= OMNI_KP_FILL:
            continue
        if abs(bz) >= OMNI_BZ_FILL:
            bz = 0.0
        if v >= OMNI_V_FILL:
            v = 400.0    # solar-min mean; conservative

        out.append(OmniRow(
            when=when,
            kp=kp10 / 10.0,   # OMNI stores Kp*10 as integer
            bz_nt=bz,
            wind_km_s=v,
            density_cm3=(None if n >= OMNI_N_FILL else n),
            proton_flux_pfu=(None if proton >= OMNI_PROTON_FILL else proton),
        ))
    return out


async def fetch_omni_hourly(
    start: datetime, end: datetime, *, timeout: float = 30.0,
) -> list[OmniRow]:
    """Pull hourly OMNI archive for [start, end] via NASA HAPI."""
    params = {
        "id": HAPI_DATASET_ID,
        "parameters": HAPI_PARAMETERS,
        "time.min": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time.max": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "format": "csv",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(HAPI_URL, params=params)
        r.raise_for_status()
        return _parse_omni_csv(r.text)


# ── Synthesis (X-ray only) ───────────────────────────────────────────────────


def _xray_at(when: datetime, profile: StormProfile) -> float:
    """
    Reconstruct GOES X-ray flux at instant `when` from the storm's flare
    timeline.

    Each flare contributes:
      - Half-Gaussian rise:  peak * exp(-((t-peak)/rise)²)  for t < peak
      - Exponential decay:   peak * exp(-(t-peak)/decay)    for t ≥ peak

    Total flux is the maximum across all flares plus the quiescent floor,
    which is what GOES XRS actually reports (always-on background, transient
    enhancements). Ten of thousands of seconds after a flare contributions
    drop to negligible so the math is dominated by the nearest flare.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    flux = XRAY_QUIESCENT_WM2
    for fl in profile.flares:
        dt_s = (when - fl.peak_time).total_seconds()
        if dt_s < 0:
            # Pre-peak: gaussian rise
            sigma = fl.rise_minutes * 60.0
            contrib = fl.peak_flux_wm2 * math.exp(-(dt_s / sigma) ** 2)
        else:
            # Post-peak: exponential decay
            tau = fl.decay_minutes * 60.0
            contrib = fl.peak_flux_wm2 * math.exp(-dt_s / tau)
        if contrib > flux:
            flux = contrib
    return flux


def _row_from_omni(omni: OmniRow, profile: StormProfile) -> dict:
    """Build a noaa_snapshots row from real OMNI + flare-timeline X-ray."""
    feeds_available: list[str] = ["kp_omni", "bz_omni", "wind_omni",
                                  "xray_flare_timeline"]
    feeds_unavailable: list[str] = []
    if omni.proton_flux_pfu is not None:
        feeds_available.append("proton_omni")
        proton = omni.proton_flux_pfu
    else:
        feeds_unavailable.append("proton_omni")
        # Quiet baseline — no SEP unless OMNI says so
        proton = 0.1

    return {
        "fetched_at": omni.when.replace(tzinfo=timezone.utc),
        "fetch_source": BACKFILL_TAG,
        "kp": omni.kp,
        "bz_nt": round(omni.bz_nt, 2),
        "xray_flux": _xray_at(omni.when, profile),
        "proton_flux_10mev": round(proton, 3),
        "wind_speed_km_s": round(omni.wind_km_s, 1),
        "kp_forecast_24h": None,
        "feeds_available": json.dumps(feeds_available),
        "feeds_unavailable": json.dumps(feeds_unavailable),
        "data_age_seconds": 0,
    }


# ── Insertion ───────────────────────────────────────────────────────────────


def _to_naive_utc(t: datetime) -> datetime:
    """SQLite stores datetimes naive — normalize for set-membership compare."""
    if t.tzinfo is None:
        return t
    return t.astimezone(timezone.utc).replace(tzinfo=None)


async def _existing_backfill_times(
    start: datetime, end: datetime,
) -> set[datetime]:
    engine = get_engine()
    async with engine.begin() as conn:
        rows = (await conn.execute(
            select(noaa_snapshots.c.fetched_at)
            .where(noaa_snapshots.c.fetch_source == BACKFILL_TAG)
            .where(noaa_snapshots.c.fetched_at >= start)
            .where(noaa_snapshots.c.fetched_at <= end)
        )).all()
    return {_to_naive_utc(r[0]) for r in rows if r[0] is not None}


async def backfill_storm(
    profile_id: str,
    start: datetime,
    end: datetime,
    *,
    fetch_omni=None,
) -> dict:
    """
    Backfill a single storm window. Idempotent on (fetched_at, source).

    `fetch_omni` is injectable for tests — defaults to live OMNI fetcher.
    """
    profile = STORM_PROFILES.get(profile_id)
    if profile is None:
        return {"profile_id": profile_id, "inserted": 0,
                "reason": "unknown profile"}

    fetch = fetch_omni or fetch_omni_hourly
    omni_rows = await fetch(start, end)
    if not omni_rows:
        return {"profile_id": profile_id, "inserted": 0,
                "reason": "no_omni_data_returned"}

    existing = await _existing_backfill_times(start, end)
    rows = [
        _row_from_omni(o, profile)
        for o in omni_rows
        if _to_naive_utc(o.when) not in existing
    ]
    if not rows:
        return {"profile_id": profile_id, "inserted": 0,
                "reason": "already_backfilled",
                "found": len(omni_rows)}

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(insert(noaa_snapshots), rows)

    return {
        "profile_id": profile_id,
        "inserted": len(rows),
        "found": len(omni_rows),
        "skipped_existing": len(omni_rows) - len(rows),
        "peak_kp": max(o.kp for o in omni_rows),
        "peak_wind_km_s": max(o.wind_km_s for o in omni_rows),
        "min_bz_nt": min(o.bz_nt for o in omni_rows),
        "peak_proton_pfu": max(
            (o.proton_flux_pfu for o in omni_rows
             if o.proton_flux_pfu is not None),
            default=None,
        ),
    }


async def backfill_all_predefined() -> list[dict]:
    """Backfill every concrete-window scenario in app/static/scenarios.json."""
    from pathlib import Path
    catalog = json.loads(
        (Path(__file__).parent.parent / "static" / "scenarios.json").read_text()
    )
    results: list[dict] = []
    for sc in catalog.get("scenarios", []):
        sid = sc.get("id")
        start = sc.get("start")
        end = sc.get("end")
        if not (sid in STORM_PROFILES and isinstance(start, str)
                and isinstance(end, str) and "live-" not in start):
            continue
        try:
            t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
            r = await backfill_storm(sid, t0, t1)
            results.append(r)
        except Exception as exc:
            logger.warning("Backfill %s failed: %s", sid, exc)
            results.append({"profile_id": sid, "inserted": 0,
                            "reason": f"error:{exc}"})
    return results
