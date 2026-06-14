"""
IonShield forecast engine.

Produces a structured 72-hour operational outlook from two sources:

1. NOAA SWPC 3-day Kp forecast (noaa-planetary-k-index-forecast.json)
   - 3-hour time blocks, ~72h horizon, updates ~4x/day
   - Contains both observed (past ~48h) and predicted (future ~72h) entries
   - This is the authoritative source for multi-hour outlook

2. 1-minute Kp trend extrapolation (sub-NOAA-resolution, IonShield-estimated)
   - Linear regression on last 15 minutes of 1-minute observed Kp
   - Projects 60 minutes ahead
   - NOT an official NOAA product — clearly labelled [ESTIMATED]
   - Useful for detecting rapid-onset conditions that haven't yet propagated
     into the 3-hour NOAA forecast cycle

Output structure:
  summary       — peak Kp, storm watch/warning flag, plain-English outlook
  windows       — 7 operational time windows (1h trend, 0-3h, 3-6h, … 48-72h)
  timeline      — full time series (past 24h + future 72h) for chart rendering

All times are UTC ISO-8601. Kp values are rounded to 2 decimal places.

Caveats (document honestly):
- Bz cannot be forecast beyond ~30-60 min (L1 solar wind propagation time)
- Kp forecast skill degrades beyond ~48h
- Storm sudden commencement (SSC) from CMEs may not appear in forecast until
  shortly before onset; monitor NOAA SWPC alerts for CME watches
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.data.noaa import _cache as noaa_cache, get_kp, data_age_seconds

logger = logging.getLogger(__name__)


# ── Risk helpers ──────────────────────────────────────────────────────────────


def _kp_to_risk(kp: float) -> str:
    if kp < 4.0:
        return "NOMINAL"
    if kp < 5.0:
        return "ELEVATED"
    if kp < 7.0:
        return "DEGRADED"
    return "SEVERE"


def _kp_to_gps_impact(kp: float, risk: str) -> str:
    """GPS impact summary string keyed to risk level."""
    return {
        "NOMINAL": "Minimal — <3 m additional L1 error",
        "ELEVATED": "Moderate — 5–15 m additional L1 error",
        "DEGRADED": "Significant — 15–30 m L1 error; precision ops affected",
        "SEVERE": "Severe — >30 m L1 error; precision GPS unreliable",
    }[risk]


def _kp_to_hf_impact(kp: float, risk: str) -> str:
    return {
        "NOMINAL": "Minimal",
        "ELEVATED": "Possible disruption at polar/sub-auroral paths",
        "DEGRADED": "Disruption likely at high latitudes; PCA risk if SEP event",
        "SEVERE": "Widespread blackout likely; all high-latitude HF circuits at risk",
    }[risk]


def _kp_to_satcom_impact(risk: str) -> str:
    return {
        "NOMINAL": "Minimal",
        "ELEVATED": "Minor scintillation possible (Ku/Ka GEO)",
        "DEGRADED": "Moderate fading expected on Ku/Ka GEO links",
        "SEVERE": "Strong fading; link outage possible on Ku/Ka GEO",
    }[risk]


# ── NOAA forecast parser ──────────────────────────────────────────────────────


def parse_kp_forecast(raw: list) -> list[dict]:
    """
    Parse NOAA noaa-planetary-k-index-forecast.json.

    NOAA serves this product as an **array of dicts** (no header row):
      {"time_tag": "2026-06-07T00:00:00", "kp": 2.67,
       "observed": "observed"|"predicted", "noaa_scale": "G1"|null}

    A legacy header-row + array-of-arrays layout is also accepted for
    resilience:
      ["time_tag", "kp", "observed", "noaa_scale"]   ← header
      ["2026-04-21 00:00:00", "2.33", "observed", ""]

    All timestamps are UTC. observed = measured; predicted = NOAA forecast.
    """
    if not raw:
        return []

    def _parse_time(v) -> datetime:
        s = str(v).strip().replace(" ", "T")
        if "+" not in s and not s.endswith("Z"):
            s += "+00:00"
        return datetime.fromisoformat(s)

    entries: list[dict] = []
    for row in raw:
        try:
            if isinstance(row, dict):
                # Native array-of-dicts format.
                if "time_tag" not in row or row.get("kp") is None:
                    continue
                ts = _parse_time(row["time_tag"])
                kp = round(float(row["kp"]), 2)
                kind = str(row.get("observed", "")).strip().lower()
            elif isinstance(row, (list, tuple)) and len(row) >= 3:
                # Legacy header+arrays format — the header row (col names) and
                # any non-numeric kp cell are skipped by the float() guard.
                ts = _parse_time(row[0])
                kp = round(float(row[1]), 2)
                kind = str(row[2]).strip().lower()
            else:
                continue
            entries.append(
                {
                    "time": ts,
                    "kp": kp,
                    "type": "forecast" if kind in ("predicted", "estimated") else "observed",
                    "risk_level": _kp_to_risk(kp),
                }
            )
        except (ValueError, TypeError, IndexError) as exc:
            logger.debug("parse_kp_forecast: skipping row %s — %s", row, exc)

    return sorted(entries, key=lambda x: x["time"])


# ── 1-minute Kp trend extrapolation ──────────────────────────────────────────


def compute_kp_trend_1h() -> Optional[float]:
    """
    Estimate Kp 1 hour ahead using linear regression on recent 1-min data.

    Method: fit a line to the last 15 minutes of 1-min Kp readings, then
    extrapolate 60 minutes forward. Clamped to [0, 9].

    This is an IonShield-internal estimate, NOT an official NOAA forecast.
    It is most useful for detecting rapid-onset substorms or sudden
    commencement events in the first hour after current conditions. Skill
    degrades beyond ~1 hour; the NOAA 3-day forecast is authoritative for
    multi-hour timescales.

    Returns None if fewer than 5 valid readings are available.
    """
    raw = noaa_cache.get("kp")
    if not raw or len(raw) < 5:
        return None

    vals: list[float] = []
    for entry in raw[-15:]:
        kp_raw = entry.get("kp_index") or entry.get("kp")
        if kp_raw is not None:
            try:
                vals.append(float(kp_raw))
            except (ValueError, TypeError):
                pass

    if len(vals) < 5:
        return None

    n = len(vals)
    x_mean = (n - 1) / 2.0
    y_mean = sum(vals) / n
    numer = sum((i - x_mean) * (vals[i] - y_mean) for i in range(n))
    denom = sum((i - x_mean) ** 2 for i in range(n))

    slope = numer / denom if abs(denom) > 1e-9 else 0.0  # Kp / minute
    kp_1h = vals[-1] + slope * 60.0
    return round(max(0.0, min(9.0, kp_1h)), 1)


# ── Window builder ────────────────────────────────────────────────────────────


def _window_kp(entries: list[dict], t_start: datetime, t_end: datetime) -> Optional[float]:
    """Mean Kp for entries whose timestamps fall in [t_start, t_end)."""
    window = [e["kp"] for e in entries if t_start <= e["time"] < t_end]
    if window:
        return round(sum(window) / len(window), 2)
    # Fall back to closest future entry
    future = [e for e in entries if e["time"] >= t_start]
    return future[0]["kp"] if future else None


def _build_window(label: str, horizon_h: float, kp: float, source: str) -> dict:
    risk = _kp_to_risk(kp)
    return {
        "label": label,
        "horizon_h": horizon_h,
        "kp_forecast": kp,
        "risk_level": risk,
        "gps_impact": _kp_to_gps_impact(kp, risk),
        "hf_impact": _kp_to_hf_impact(kp, risk),
        "satcom_impact": _kp_to_satcom_impact(risk),
        "source": source,
    }


# ── Outlook text generator ────────────────────────────────────────────────────


def _outlook_text(
    current_kp: float,
    max_kp_24h: float,
    max_kp_72h: float,
    peak_time: Optional[datetime],
    now: datetime,
) -> str:
    """Plain-English operational outlook for mission planners."""

    if max_kp_72h < 4.0:
        return (
            "Quiet to unsettled conditions expected across the 72-hour window. "
            "No significant operational impacts forecast."
        )

    g_level = min(5, int(max_kp_72h) - 4) if max_kp_72h >= 5 else 0

    if max_kp_72h >= 7:
        severity = f"G{g_level} geomagnetic storm"
        impact = (
            "Significant GPS degradation expected at all latitudes. "
            "HF blackout likely at high latitudes. "
            "GPS-dependent operations should be delayed or augmented with INS backup."
        )
    elif max_kp_72h >= 5:
        severity = "G1 storm conditions"
        impact = (
            "Elevated GPS error (5–15 m L1) and HF disruption at high latitudes. "
            "Monitor conditions; activate backup navigation if precision is critical."
        )
    else:
        severity = "Active geomagnetic conditions"
        impact = "Minor GPS degradation possible. Standard precautions advised."

    if peak_time:
        dt_h = (peak_time - now).total_seconds() / 3600.0
        if dt_h < 0:
            timing = "currently in progress"
        elif dt_h < 3:
            timing = "onset within 3 hours"
        elif dt_h < 24:
            timing = f"expected in ~{dt_h:.0f} hours"
        else:
            timing = f"expected in ~{dt_h / 24:.1f} days"
        return f"{severity} (peak Kp {max_kp_72h:.1f}) {timing}. {impact}"

    return f"{severity} (peak Kp {max_kp_72h:.1f}) forecast within 72 hours. {impact}"


# ── Master forecast builder ───────────────────────────────────────────────────


def build_forecast() -> dict:
    """
    Build the complete IonShield forecast response.

    Combines the NOAA 3-day Kp forecast with a 1-hour trend estimate derived
    from real-time 1-minute Kp observations.
    """
    now = datetime.now(timezone.utc)
    raw = noaa_cache.get("kp_forecast")
    current_kp = get_kp()

    # Parse NOAA forecast
    all_entries = parse_kp_forecast(raw) if raw else []

    # Split observed (past) and forecast (future) by type label, then by time
    forecast_entries = [e for e in all_entries if e["type"] == "forecast" or e["time"] > now]
    if not forecast_entries:
        forecast_entries = [e for e in all_entries if e["time"] > now]

    has_noaa = len(forecast_entries) > 0

    # ── 1h trend ──────────────────────────────────────────────────────────
    kp_1h = compute_kp_trend_1h()
    kp_1h_risk = _kp_to_risk(kp_1h) if kp_1h is not None else _kp_to_risk(current_kp)

    # ── Operational time windows ──────────────────────────────────────────
    windows: list[dict] = []

    # 1-hour trend (sub-NOAA resolution, estimated)
    windows.append(
        _build_window(
            label="Next 1h",
            horizon_h=1.0,
            kp=kp_1h if kp_1h is not None else current_kp,
            source="[ESTIMATED] Linear trend from 1-min Kp data",
        )
    )

    # NOAA 3-hour windows (official)
    noaa_source = "NOAA SWPC 3-day Kp forecast"
    for label, h0, h1 in [
        ("0–3h", 0, 3),
        ("3–6h", 3, 6),
        ("6–12h", 6, 12),
        ("12–24h", 12, 24),
        ("24–48h", 24, 48),
        ("48–72h", 48, 72),
    ]:
        t0 = now + timedelta(hours=h0)
        t1 = now + timedelta(hours=h1)
        kp = _window_kp(forecast_entries, t0, t1) or current_kp
        windows.append(_build_window(label, h1, kp, noaa_source))

    # ── Summary statistics ────────────────────────────────────────────────
    future_72h = [e for e in forecast_entries if e["time"] <= now + timedelta(hours=72)]
    future_24h = [e for e in forecast_entries if e["time"] <= now + timedelta(hours=24)]

    kps_72h = [e["kp"] for e in future_72h]
    kps_24h = [e["kp"] for e in future_24h]

    max_kp_72h = round(max(kps_72h), 1) if kps_72h else current_kp
    max_kp_24h = round(max(kps_24h), 1) if kps_24h else current_kp

    peak_entry = max(future_72h, key=lambda e: e["kp"], default=None)
    peak_time = peak_entry["time"] if peak_entry else None
    hours_to_peak = round((peak_time - now).total_seconds() / 3600, 1) if peak_time else None

    storm_watch = max_kp_72h >= 5.0
    storm_warning = max_kp_72h >= 7.0
    storm_level = f"G{min(5, int(max_kp_72h) - 4)}" if max_kp_72h >= 5 else None

    # ── Timeline (past 24h observed + next 72h forecast) ──────────────────
    t_start = now - timedelta(hours=24)
    timeline = [
        {
            "time": e["time"].isoformat(),
            "kp": e["kp"],
            "type": e["type"],
            "risk_level": e["risk_level"],
        }
        for e in all_entries
        if e["time"] >= t_start
    ]

    # If no near-term NOAA forecast data, splice in the 1h trend point
    if kp_1h is not None:
        nearest_future = min(
            (e["time"] for e in forecast_entries),
            default=now + timedelta(hours=24),
        )
        if nearest_future > now + timedelta(hours=2):
            timeline.append(
                {
                    "time": (now + timedelta(hours=1)).isoformat(),
                    "kp": kp_1h,
                    "type": "trend_estimate",
                    "risk_level": kp_1h_risk,
                }
            )
            timeline.sort(key=lambda x: x["time"])

    return {
        "generated": now.isoformat(),
        "current_kp": round(current_kp, 1),
        "kp_trend_1h": kp_1h,
        "kp_trend_1h_risk": kp_1h_risk,
        "forecast_source": noaa_source if has_noaa else "fallback (NOAA forecast unavailable)",
        "data_age_seconds": data_age_seconds(),
        "summary": {
            "max_kp_24h": max_kp_24h,
            "max_kp_72h": max_kp_72h,
            "max_risk_24h": _kp_to_risk(max_kp_24h),
            "max_risk_72h": _kp_to_risk(max_kp_72h),
            "peak_kp": max_kp_72h,
            "peak_time": peak_time.isoformat() if peak_time else None,
            "hours_to_peak": hours_to_peak,
            "storm_watch": storm_watch,
            "storm_warning": storm_warning,
            "storm_level": storm_level,
            "outlook_text": _outlook_text(current_kp, max_kp_24h, max_kp_72h, peak_time, now),
        },
        "windows": windows,
        "timeline": timeline,
    }
