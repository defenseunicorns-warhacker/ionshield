"""
Impact modeling — physics-informed translation of FusedObservations into
operational system metrics.

Outputs are intentionally **multi-system** and **multi-band** so a downstream
consumer (mission planner, dashboard, Foundry pipeline) can pick whichever
slice is relevant without re-running the calculation.

The math here intentionally re-uses the heuristics in app.models.risk — but
operates on a single FusedObservation (no globals), uses real GloTEC TEC
where available (no latitude-bucketed climatology), and emits a clean
dataclass tree. That swap is what lets impact assessment be:

  - deterministic and replayable from any historical FusedObservation
  - per-region (one assessment per cell in the global grid, not just one
    global scalar like risk.compute_risk produced)
  - testable without mocking the NOAA cache

Physics references — same as risk.py:
  GPS:    Klobuchar 1996 (ionospheric delay), Mannucci 2005 (storm enhancement)
  HF:     CCIR-888 (SID), Rose & Ziauddin 1962 (storm), Bailey 1964 (PCA)
  SATCOM: ITU-R P.531-14 (Nakagami fading), Fremouw & Rino 1973 (strong scint)
  Radar:  Skolnik (group delay 40.3·VTEC/f²), CCIR (Faraday rotation)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.ontology import FusedObservation, Region


# ── Per-system result types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class GpsImpact:
    asset_type: str          # GPS_L1 | GPS_L1L2 | GPS_L1L5 | GPS_INS | SBAS
    error_m: float
    error_low_m: float
    error_high_m: float
    vtec_used_tecu: float
    iono_correction_active: bool


@dataclass(frozen=True)
class HfImpact:
    absorption_total_db: float
    absorption_sid_db: float        # X-ray driven (dayside only)
    absorption_storm_db: float      # Kp driven (geomag latitude scaled)
    absorption_pca_db: float        # proton driven (polar only)
    blackout_probability: float     # P[total_abs > 25 dB fade margin]
    is_dayside: bool
    pca_active: bool


@dataclass(frozen=True)
class SatcomImpact:
    band: str                       # "L" | "Ku"
    fade_db: float
    outage_probability: float
    s4_used: float


@dataclass(frozen=True)
class RadarImpact:
    band: str                       # "L" | "S" | "C" | "X"
    range_bias_m: float
    coherence_degraded: bool


@dataclass(frozen=True)
class ImpactAssessment:
    region: Region
    when: datetime
    gps: dict[str, GpsImpact]
    hf: HfImpact
    satcom: dict[str, SatcomImpact]
    radar: dict[str, RadarImpact]

    def to_rows(self) -> list[dict]:
        """
        Flatten to one row per (region, system, band) suitable for a
        relational Foundry dataset. Each row carries the same region_id
        + when so it can be joined with FusedObservation rows by key.
        """
        when_iso = self.when.isoformat()
        out: list[dict] = []
        base = {
            "region_id": self.region.region_id,
            "lat_deg": self.region.lat_deg,
            "lon_deg": self.region.lon_deg,
            "geomag_lat_deg": self.region.geomag_lat_deg,
            "when_utc": when_iso,
        }
        for asset, g in self.gps.items():
            out.append({**base, "system": "GPS", "subsystem": asset,
                        "metric": "error_m", "value": g.error_m,
                        "value_low": g.error_low_m, "value_high": g.error_high_m,
                        "vtec_tecu": g.vtec_used_tecu,
                        "iono_correction_active": g.iono_correction_active})
        out.append({**base, "system": "HF", "subsystem": "TOTAL",
                    "metric": "absorption_db", "value": self.hf.absorption_total_db,
                    "abs_sid_db": self.hf.absorption_sid_db,
                    "abs_storm_db": self.hf.absorption_storm_db,
                    "abs_pca_db": self.hf.absorption_pca_db,
                    "blackout_probability": self.hf.blackout_probability,
                    "dayside": self.hf.is_dayside,
                    "pca_active": self.hf.pca_active})
        for band, s in self.satcom.items():
            out.append({**base, "system": "SATCOM", "subsystem": band,
                        "metric": "fade_db", "value": s.fade_db,
                        "outage_probability": s.outage_probability,
                        "s4": s.s4_used})
        for band, r in self.radar.items():
            out.append({**base, "system": "RADAR", "subsystem": band,
                        "metric": "range_bias_m", "value": r.range_bias_m,
                        "coherence_degraded": r.coherence_degraded})
        return out


# ── Helpers ──────────────────────────────────────────────────────────────────


def _solar_zenith_angle(lat_deg: float, lon_deg: float, when: datetime) -> float:
    """
    Solar zenith angle in degrees at (lat, lon) at UTC instant `when`.

    Standard NOAA equation-of-time approximation; accurate to ~0.5° for
    operational use. Returns 0–180; values > 90 are night.
    """
    # Day of year (1–366) and fractional UTC hour
    doy = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60.0 + when.second / 3600.0

    # Solar declination
    decl = -23.44 * math.cos(math.radians(360.0 / 365.0 * (doy + 10)))
    # Equation of time (minutes), small correction
    B = math.radians(360.0 / 365.0 * (doy - 81))
    eot = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    # Solar time
    solar_hour = hour + lon_deg / 15.0 + eot / 60.0
    h_angle = (solar_hour - 12.0) * 15.0  # degrees

    lat_r = math.radians(lat_deg)
    decl_r = math.radians(decl)
    h_r = math.radians(h_angle)
    cos_z = (
        math.sin(lat_r) * math.sin(decl_r)
        + math.cos(lat_r) * math.cos(decl_r) * math.cos(h_r)
    )
    cos_z = max(-1.0, min(1.0, cos_z))
    return math.degrees(math.acos(cos_z))


# Ionospheric correction factors per asset type (from risk.py ASSET_IONO_FACTOR)
ASSET_IONO_FACTOR: dict[str, float] = {
    "GPS_L1": 1.00,
    "GPS_L1L2": 0.05,    # dual-freq cancels first-order TEC
    "GPS_L1L5": 0.05,
    "GPS_INS": 0.40,     # INS bridges short outages
    "SBAS": 0.10,        # WAAS corrections (degrade in storms — see compute_gps)
}

NON_IONO_FLOOR_M: float = 1.5

# Constants for ionospheric delay calculations
TEC_TO_L1_M_PER_TECU: float = 0.162  # 40.3e16 / f1^2, f1 = 1.57542 GHz
OBLIQUITY_FACTOR: float = 2.5        # mixed-elevation fleet average
SCINT_NOISE_GAIN: float = 18.0       # dB-equivalent above S4=0.30
HF_FADE_MARGIN_DB: float = 25.0      # conservative HF link budget


# ── GPS impact ───────────────────────────────────────────────────────────────


def _vtec_estimate(obs: FusedObservation) -> float:
    """
    Use the FusedObservation's TEC value as authoritative.

    Fusion is the single source of truth: when GloTEC is live it carries the
    real interpolated value; when GloTEC is unavailable fusion populates the
    climatology fallback. Impact assessment trusts that input rather than
    second-guessing it — keeps the contract clean and tests deterministic.
    """
    return obs.tec_tecu


def _s4_estimate(obs: FusedObservation) -> float:
    """
    Estimate S4 amplitude scintillation index from environmental drivers.

    Auroral oval (50–70° geomag) and equatorial anomaly (|geomag| < 20°)
    are the two scintillation regimes; mid-latitude is generally quiet
    (S4 < 0.1) outside major storms.

    Functional form mirrors risk.compute_s4 but driven from FusedObservation
    fields instead of globals.
    """
    region = obs.region
    kp = obs.kp_index

    if region.is_auroral and kp >= 5:
        s4 = 0.20 + (kp - 5) * 0.18
    elif region.is_polar and kp >= 6:
        s4 = 0.30 + (kp - 6) * 0.10
    elif region.is_equatorial:
        # Equatorial post-sunset bubbles — proxy via TEC anomaly
        s4 = 0.05 + min(0.50, max(0.0, obs.tec_anomaly_tecu) * 0.04)
    else:
        # Mid-latitude background, gently elevated by Kp
        s4 = 0.02 + max(0.0, kp - 4) * 0.04
    return min(0.95, s4)


def assess_gps(obs: FusedObservation, asset_type: str = "GPS_L1") -> GpsImpact:
    """Per-asset GPS positional error in meters."""
    vtec = _vtec_estimate(obs)
    s4 = _s4_estimate(obs)

    l1_vert = TEC_TO_L1_M_PER_TECU * vtec
    scint_noise = max(0.0, (s4 - 0.30) * SCINT_NOISE_GAIN) if s4 > 0.30 else 0.0
    l1_total = l1_vert * OBLIQUITY_FACTOR + scint_noise

    factor = ASSET_IONO_FACTOR.get(asset_type, 1.0)
    if asset_type == "SBAS" and obs.kp_index >= 5:
        factor = min(0.9, factor + 0.12 * (obs.kp_index - 5.0))

    corrected = l1_total * factor
    total = max(NON_IONO_FLOOR_M, corrected + NON_IONO_FLOOR_M)

    return GpsImpact(
        asset_type=asset_type,
        error_m=round(total, 2),
        error_low_m=round(total * 0.7, 2),
        error_high_m=round(total * 1.6, 2),
        vtec_used_tecu=round(vtec, 1),
        iono_correction_active=factor < 1.0,
    )


# ── HF impact ────────────────────────────────────────────────────────────────


def assess_hf(obs: FusedObservation) -> HfImpact:
    sza = _solar_zenith_angle(
        obs.region.lat_deg, obs.region.lon_deg, obs.when
    )
    cos_sza = max(0.0, math.cos(math.radians(sza)))
    is_dayside = cos_sza > 0.1

    # 1. SID — dayside only, X-ray driven
    if obs.xray_flux_wm2 > 1e-7 and cos_sza > 0.0:
        sid = min(40.0, 18.0 * math.sqrt(obs.xray_flux_wm2 / 1e-6) * cos_sza)
    else:
        sid = 0.0

    # 2. Geomagnetic storm absorption — geomag latitude scaled
    a = abs(obs.region.geomag_lat_deg)
    if a > 65:
        storm = obs.kp_index * 3.0
    elif a > 55:
        storm = obs.kp_index * 1.5
    elif a > 25:
        storm = obs.kp_index * 0.3
    else:
        storm = obs.kp_index * 0.1

    # 3. PCA — polar latitudes, proton flux driven
    pca = 0.0
    pca_active = False
    if a > 65 and obs.proton_flux_10mev_pfu >= 10.0:
        s_level = math.log10(obs.proton_flux_10mev_pfu / 10.0)
        pca = min(50.0, 10.0 * (1.0 + s_level))
        pca_active = True

    total = sid + storm + pca
    blackout_p = round(min(1.0, total / HF_FADE_MARGIN_DB), 2)

    return HfImpact(
        absorption_total_db=round(total, 2),
        absorption_sid_db=round(sid, 2),
        absorption_storm_db=round(storm, 2),
        absorption_pca_db=round(pca, 2),
        blackout_probability=blackout_p,
        is_dayside=is_dayside,
        pca_active=pca_active,
    )


# ── SATCOM impact ────────────────────────────────────────────────────────────


def assess_satcom(obs: FusedObservation) -> dict[str, SatcomImpact]:
    """
    SATCOM fade by band:
      L-band (1–2 GHz): scintillation-prone in auroral / equatorial regions
      Ku-band (12–18 GHz): largely scint-immune; rain fade dominates instead
                            (we don't model rain here — return 0 fade)

    Fade depth (Nakagami-m for moderate scint, empirical for strong scint):
        weak/mod:  Fade_dB = -10 log10(1 - S4²)
        strong:    Fade_dB = 3 + S4 × 24
    Outage probability uses the conservative 3 dB margin from risk.py.
    """
    s4 = _s4_estimate(obs)
    if s4 <= 0.01:
        l_fade = 0.0
    elif s4 < 0.6:
        l_fade = min(15.0, -10.0 * math.log10(max(1e-3, 1.0 - s4 ** 2)))
    else:
        l_fade = min(22.0, 3.0 + s4 * 24.0)
    l_outage = round(min(1.0, max(0.0, (l_fade - 0.5) / 12.0)), 2)

    return {
        "L": SatcomImpact(band="L", fade_db=round(l_fade, 2),
                          outage_probability=l_outage, s4_used=round(s4, 3)),
        "Ku": SatcomImpact(band="Ku", fade_db=0.0,
                           outage_probability=0.0, s4_used=round(s4, 3)),
    }


# ── Radar impact ─────────────────────────────────────────────────────────────


# Range bias scales as 40.3·VTEC/f² — coefficients per band (relative to L)
_BAND_FREQ_HZ: dict[str, float] = {
    "L": 1.3e9, "S": 3.0e9, "C": 5.5e9, "X": 10.0e9,
}


def assess_radar(obs: FusedObservation) -> dict[str, RadarImpact]:
    vtec = _vtec_estimate(obs)
    s4 = _s4_estimate(obs)
    coherence_lost = s4 > 0.30
    out: dict[str, RadarImpact] = {}
    for band, f in _BAND_FREQ_HZ.items():
        bias = 40.3e16 * vtec / (f * f)
        out[band] = RadarImpact(
            band=band, range_bias_m=round(bias, 3),
            coherence_degraded=coherence_lost,
        )
    return out


# ── Top-level assessment ─────────────────────────────────────────────────────


GPS_ASSET_TYPES: tuple[str, ...] = (
    "GPS_L1", "GPS_L1L2", "GPS_L1L5", "GPS_INS", "SBAS",
)


def assess_region(obs: FusedObservation) -> ImpactAssessment:
    """Build a full ImpactAssessment for a single FusedObservation."""
    return ImpactAssessment(
        region=obs.region,
        when=obs.when,
        gps={a: assess_gps(obs, a) for a in GPS_ASSET_TYPES},
        hf=assess_hf(obs),
        satcom=assess_satcom(obs),
        radar=assess_radar(obs),
    )


def assess_grid(grid: list[FusedObservation]) -> list[ImpactAssessment]:
    """Vectorize assess_region over an entire fused grid."""
    return [assess_region(obs) for obs in grid]
