"""
IonShield physics / operational risk engine — v3.

Translates real-time solar-terrestrial indices into operationally meaningful
impact estimates for GPS navigation, HF communications, SATCOM, and radar.

All models are approximations. Where a full model (e.g., IRI, WBMOD, CCIR-533)
would require numerical integration or lookup tables, we use documented
analytical approximations. Provenance and assumptions are noted inline.

Inputs consumed from noaa.py:
  Kp          — planetary geomagnetic index (0–9)
  Bz          — IMF southward component in nT (key storm driver)
  X-ray flux  — GOES 1–8 Å (W/m²); flare classification
  Proton flux — GOES ≥10 MeV integral (pfu); polar cap absorption driver

Geographic inputs:
  lat, lon    — decimal degrees
  asset_type  — GPS receiver capability (affects iono-correction factor)
"""

import math
from datetime import datetime, timezone

from app.data.noaa import (
    get_kp,
    get_xray_flux,
    get_wind_speed,
    get_bz,
    get_proton_flux_10mev,
    data_age_seconds,
)

# ── Asset type iono-correction factors ───────────────────────────────────────
# Dual-frequency GPS removes ionospheric delay via the iono-free combination
# (LC = f1²·P1 − f2²·P2)/(f1² − f2²), eliminating ~95% of first-order error.
# Single-frequency L1 carries the full delay. SBAS provides partial correction
# via ground-network TEC maps; corrections degrade during rapid storm-time
# TEC gradients (Kp ≥ 5, per WAAS MOPS DO-229E).
ASSET_IONO_FACTOR: dict[str, float] = {
    "GPS_L1": 1.00,  # full iono error — civilian standard
    "GPS_L1L2": 0.05,  # ~95% reduction via iono-free combination (IS-GPS-200)
    "GPS_L1L5": 0.05,  # same as L1/L2 with modernized signal
    "GPS_INS": 0.40,  # INS absorbs short-term iono, but re-initialises on GPS fix
    "SBAS": 0.30,  # partial correction; factor degrades during storms (see below)
}
DEFAULT_ASSET = "GPS_L1"

# ── Known military installations ─────────────────────────────────────────────
BASES: list[dict] = [
    {"name": "Thule AB, Greenland", "lat": 76.5, "lon": -68.7},
    {"name": "Clear SFS, Alaska", "lat": 64.3, "lon": -149.2},
    {"name": "Schriever SFB, CO", "lat": 38.8, "lon": -104.5},
    {"name": "Vandenberg SFB, CA", "lat": 34.7, "lon": -120.6},
    {"name": "Cape Canaveral, FL", "lat": 28.5, "lon": -80.6},
    {"name": "Diego Garcia", "lat": -7.3, "lon": 72.4},
    {"name": "Ramstein AB, Germany", "lat": 49.4, "lon": 7.6},
    {"name": "Kadena AB, Japan", "lat": 26.4, "lon": 127.8},
    {"name": "Camp Humphreys, ROK", "lat": 36.9, "lon": 127.0},
    {"name": "Al Udeid AB, Qatar", "lat": 25.1, "lon": 51.3},
]


# ── Geometry helpers ─────────────────────────────────────────────────────────


def local_solar_time(lon: float) -> float:
    """Approximate local solar time in hours (0–24)."""
    now = datetime.now(timezone.utc)
    return (now.hour + now.minute / 60.0 + lon / 15.0) % 24.0


def solar_zenith_angle(lat: float, lon: float) -> float:
    """
    Solar zenith angle in degrees.

    Uses Spencer (1971) solar declination and standard hour-angle formula.
    Accuracy ±1° for most of the year; sufficient for HF absorption estimates.
    """
    now = datetime.now(timezone.utc)
    doy = now.timetuple().tm_yday
    # Solar declination (degrees) — Spencer 1971
    decl = 23.45 * math.sin(math.radians(360.0 / 365.0 * (doy - 81)))
    lst = local_solar_time(lon)
    ha = (lst - 12.0) * 15.0  # hour angle in degrees
    cos_sza = math.sin(math.radians(lat)) * math.sin(math.radians(decl)) + math.cos(
        math.radians(lat)
    ) * math.cos(math.radians(decl)) * math.cos(math.radians(ha))
    # Clamp to valid range before acos
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sza))))


def lat_zone(lat: float) -> dict:
    """
    Classify latitude into ionospheric regime and assign zone multiplier.

    Multipliers reflect enhanced variability relative to quiet mid-latitude
    baseline (1.0). Values are empirically motivated but simplified:
      Polar (>70°):        strong particle precipitation, auroral currents → 1.8×
      Sub-auroral (55–70°): auroral oval boundary, TEC gradients → 1.3×
      Mid-latitude (25–55°): generally well-behaved; lowest variability → 1.0×
      Equatorial (<25°):   plasma bubble instabilities, EIA → 1.4×

    Note: real longitudinal variation (e.g., South Atlantic Anomaly, EIA crests
    at ±15° magnetic lat) is not captured here.
    """
    a = abs(lat)
    if a > 70:
        return {"zone": "polar", "multiplier": 1.8}
    if a > 55:
        return {"zone": "sub-auroral", "multiplier": 1.3}
    if a > 25:
        return {"zone": "mid-latitude", "multiplier": 1.0}
    return {"zone": "equatorial", "multiplier": 1.4}


# ── S4 scintillation index ───────────────────────────────────────────────────


def compute_s4(lat: float, lon: float, kp: float) -> float:
    """
    Estimate amplitude scintillation index S4 (dimensionless, 0–1).

    S4 = sqrt(Var(I)) / <I>  where I is received signal intensity.

    Two physically distinct scintillation regimes:

    1. EQUATORIAL (|lat| < 20°): Rayleigh-Taylor plasma bubble instability.
       - Occurs post-sunset ~19:00–02:00 LT when the F-layer rises and
         bottomside density gradients invert (Kelvin-Helmholtz analog).
       - Kp has a weak, indirect effect on equatorial scintillation.
       - Ref: Basu et al. 1988 (functional form simplified); Abdu et al. 2003.

    2. HIGH-LATITUDE (|lat| > 55°): Geomagnetically-driven irregularities in
       polar/auroral ionosphere. Strongly correlated with Kp.
       - Polar cap: additional contribution from SEP proton precipitation.
       - Ref: Aquino et al. 2005 (analytical approximation).

    Mid-latitude: generally quiet (S4 < 0.15) except during severe storms
    (Kp ≥ 7) when storm-enhanced density (SED) can trigger irregularities.

    Limitations: no F10.7 (solar cycle) dependence, no season, no longitude
    variation. Conservative at mid-latitudes; may underestimate equatorial
    scintillation at solar maximum.
    """
    a = abs(lat)
    lst = local_solar_time(lon)

    if a > 70:
        # Polar cap: Kp-driven + SEP proton contribution
        proton = get_proton_flux_10mev()
        # Proton contribution is significant above S1 (10 pfu) threshold
        proton_contrib = min(0.25, 0.06 * math.log10(max(proton, 0.1) + 1))
        night_factor = 1.3 if (lst < 6 or lst > 20) else 1.0
        s4 = min(1.0, (0.05 + kp * 0.07 + proton_contrib) * night_factor)

    elif a > 55:
        # Sub-auroral / auroral oval: Kp-driven, night-enhanced
        night_factor = 1.2 if (lst < 6 or lst > 21) else 1.0
        s4 = min(1.0, (0.02 + kp * 0.06) * night_factor)

    elif a > 25:
        # Mid-latitude: generally very low; only notable during severe storms
        if kp >= 7:
            s4 = min(0.4, 0.02 + (kp - 6) * 0.06)
        else:
            s4 = min(0.12, 0.005 + kp * 0.008)

    else:
        # Equatorial: plasma bubble regime
        # Post-sunset window ~19:00–02:00 LT; strong enhancement
        post_sunset = lst >= 19.0 or lst <= 2.0
        evening_factor = 2.2 if post_sunset else 0.4
        s4 = min(1.0, (0.04 + kp * 0.015) * evening_factor)

    return round(s4, 3)


# ── GPS error model ──────────────────────────────────────────────────────────


def compute_gps_error(
    lat: float, s4: float, kp: float, asset_type: str = DEFAULT_ASSET
) -> dict:
    """
    Estimate GPS positioning error in metres.

    For L1-only GPS the dominant storm-time error source is ionospheric delay:

        ΔI_vertical = (40.3 × 10¹⁶ × VTEC) / f₁²   [metres]

    where VTEC is vertical total electron content (TECU = 10¹⁶ el/m²) and
    f₁ = 1.57542 × 10⁹ Hz → coefficient ≈ 0.162 m/TECU.

    VTEC baseline by latitude (quiet solar-min values, Bilitza IRI-2016):
      Equatorial: ~15 TECU  Mid-lat: ~10 TECU  Polar: ~6 TECU

    Storm-time VTEC enhancement above Kp = 4 (empirical, Mannucci et al. 2005):
      factor ≈ 1 + 0.40 × (Kp − 4)  for Kp > 4

    Position error from vertical delay uses obliquity factor ~2.5, which
    accounts for the fact that satellites are observed at a range of elevations
    (not just zenith). Conservative estimate for mixed-elevation constellation.

    Scintillation (S4 > 0.3) adds pseudorange noise via rapid phase/amplitude
    fluctuations and potential cycle slips. Empirical from Van Dierendonck 1999.

    Non-ionospheric floor: ~1.5 m (tropospheric ~0.5m, multipath ~0.7m,
    receiver noise ~0.3m) — consistent with IS-GPS-200 non-SA residuals.

    Asset-type correction factors applied after L1 iono error is computed.
    SBAS correction degrades for Kp ≥ 5 (WAAS MOPS DO-229E, Table 2-3).
    """
    a = abs(lat)

    # Quiet VTEC baseline (TECU)
    if a < 20:
        baseline_vtec = 15.0
    elif a < 55:
        baseline_vtec = 10.0
    else:
        baseline_vtec = 6.0

    # Storm-time enhancement
    storm_mult = 1.0 + max(0.0, (kp - 4.0) * 0.40)
    total_vtec = baseline_vtec * storm_mult

    # Vertical L1 iono delay (m)
    l1_vert = 0.162 * total_vtec

    # Obliquity factor (satellite elevation geometry)
    obliquity = 2.5

    # Scintillation pseudorange noise (empirical, Van Dierendonck 1999)
    scint_noise = max(0.0, (s4 - 0.30) * 18.0) if s4 > 0.30 else 0.0

    l1_total = l1_vert * obliquity + scint_noise

    # Asset-type iono correction
    factor = ASSET_IONO_FACTOR.get(asset_type, 1.0)
    if asset_type == "SBAS" and kp >= 5:
        # SBAS corrections degrade during storm-time TEC gradients
        factor = min(0.9, factor + 0.12 * (kp - 5.0))

    corrected = l1_total * factor

    # Add non-iono floor
    total_error = round(max(1.5, corrected + 1.5), 1)

    return {
        "gps_error_m": total_error,
        "gps_error_range": [round(total_error * 0.7, 1), round(total_error * 1.6, 1)],
        "vtec_estimate_tecu": round(total_vtec, 1),
        "asset_type": asset_type,
        "iono_correction_active": factor < 1.0,
    }


# ── HF communications risk ───────────────────────────────────────────────────


def compute_hf_risk(lat: float, lon: float, kp: float) -> dict:
    """
    Estimate HF communications degradation.

    Three independent mechanisms:

    1. SUDDEN IONOSPHERIC DISTURBANCE (SID) — X-ray flare → enhanced D-layer
       ionisation on the sunlit hemisphere. Absorption (dB) follows:
           A_SID ≈ K_f × sqrt(Φ_X / 10⁻⁶) × cos(SZA)
       where K_f ≈ 18 dB normalised to 10 MHz (C1 ~ 1–3 dB, M1 ~ 5 dB,
       X1 ~ 15–20 dB at overhead sun). Ref: CCIR-888 functional form.

    2. GEOMAGNETIC STORM ABSORPTION — disturbed auroral electrojets and
       particle precipitation enhance D/E-layer absorption at high latitudes:
           A_storm ≈ Kp × zone_factor  (dB, empirical)
       Ref: Rose & Ziauddin 1962 (simplified by zone).

    3. POLAR CAP ABSORPTION (PCA) — solar energetic protons (≥10 MeV) cause
       severe D-layer ionisation poleward of ~65° geomagnetic lat. Essentially
       blackouts HF paths through the polar cap during S2+ events.
           A_PCA ≈ 10 × (1 + log₁₀(flux/10))  dB   for flux ≥ 10 pfu
       Ref: Bailey 1964, simplified.

    Blackout probability: fraction of total absorption relative to typical
    HF link fade margin (25 dB — conservative working assumption).
    Paths with < 25 dB margin will see outage proportionally sooner.

    Frequency note: output normalised to ~10 MHz. Absorption scales as ~1/f²
    so 5 MHz circuits face ~4× this absorption; 20 MHz ~¼.
    """
    xray_flux = get_xray_flux()
    proton_flux = get_proton_flux_10mev()
    sza = solar_zenith_angle(lat, lon)
    a = abs(lat)

    # 1. SID — dayside only
    cos_sza = max(0.0, math.cos(math.radians(sza)))
    if xray_flux > 1e-7 and cos_sza > 0.0:
        sid_abs = min(40.0, 18.0 * math.sqrt(xray_flux / 1e-6) * cos_sza)
    else:
        sid_abs = 0.0

    # 2. Geomagnetic storm absorption (zone-dependent)
    if a > 65:
        storm_abs = kp * 3.0
    elif a > 55:
        storm_abs = kp * 1.5
    elif a > 25:
        storm_abs = kp * 0.3
    else:
        storm_abs = kp * 0.1  # equatorial: MUF shift matters more than D-layer

    # 3. PCA (polar latitudes only, ≥10 pfu threshold)
    pca_abs = 0.0
    if a > 65 and proton_flux >= 10.0:
        s_level = math.log10(proton_flux / 10.0)  # 0 at S1, 1 at S2, 2 at S3
        pca_abs = min(50.0, 10.0 * (1.0 + s_level))

    total_abs = sid_abs + storm_abs + pca_abs

    # Blackout probability (25 dB assumed fade margin)
    blackout_prob = round(min(1.0, total_abs / 25.0), 2)

    return {
        "hf_absorption_db": round(total_abs, 1),
        "hf_sid_db": round(sid_abs, 1),
        "hf_storm_db": round(storm_abs, 1),
        "hf_pca_db": round(pca_abs, 1),
        "hf_blackout_probability": blackout_prob,
        "solar_zenith_deg": round(sza, 1),
        "hf_dayside": cos_sza > 0.1,
        "pca_active": pca_abs > 0.0,
    }


# ── SATCOM scintillation risk ────────────────────────────────────────────────


def compute_satcom_risk(s4: float) -> dict:
    """
    Estimate SATCOM signal fading from amplitude scintillation.

    Applies to GEO Ku/Ka-band SATCOM. L-band (MUOS, INMARSAT) is significantly
    more robust and these estimates will over-predict fade for L-band.

    Fade depth uses the Nakagami-m distribution (m ≈ 1/S4² for strong scint):
        Fade_dB (90th percentile) ≈ −10 × log₁₀(1 − S4²)   [weak/mod scint]
    For S4 ≥ 0.6 (strong), the Nakagami model underestimates tails; we blend
    to the empirical upper bound from Fremouw & Rino 1973:
        Fade_dB ≈ 3 + S4 × 24

    Link outage probability assumes 3 dB Ku-band fade margin (conservative;
    typical terminal fade margins are 3–8 dB). Adjust for your system.

    Ref: ITU-R P.531-14 (functional form); Fremouw & Rino 1973.
    """
    if s4 <= 0.01:
        fade_db = 0.0
    elif s4 < 0.60:
        denominator = max(1e-3, 1.0 - s4**2)
        fade_db = min(15.0, -10.0 * math.log10(denominator))
    else:
        # Strong scintillation — empirical blend
        fade_db = min(22.0, 3.0 + s4 * 24.0)

    # Outage probability at 3 dB system fade margin
    outage_prob = round(min(1.0, max(0.0, (fade_db - 0.5) / 12.0)), 2)

    return {
        "satcom_fade_db": round(fade_db, 1),
        "satcom_outage_probability": outage_prob,
        "satcom_applies_to": "Ku/Ka GEO SATCOM (L-band significantly more robust)",
    }


# ── Radar / sensing impact ───────────────────────────────────────────────────


def compute_radar_impact(kp: float, s4: float, lat: float) -> dict:
    """
    Estimate ionospheric effects on radar / sensing.

    Primary mechanisms:
    1. GROUP DELAY (range bias): ΔR = 40.3×10¹⁶ × VTEC / f²  [m]
       Using same VTEC estimates as GPS section. Normalised to L-band (1.3 GHz).
       X-band (10 GHz): scale by (1.3/10)² ≈ 0.017 → ~58× less affected.

    2. FARADAY ROTATION: polarisation rotation ∝ TEC / f²; significant
       at L-band (< ~3 GHz) for linearly polarised antennas.

    3. COHERENT INTEGRATION LOSS: rapid phase fluctuations (S4 > 0.3) limit
       coherent dwell time → reduced Doppler resolution and SNR gain.

    This is a simplified range-bias / coherence impact assessment.
    Full SAR/ISAR ionospheric compensation is beyond scope here.
    """
    a = abs(lat)
    zone = lat_zone(lat)
    m = zone["multiplier"]

    if a < 20:
        baseline_vtec = 15.0
    elif a < 55:
        baseline_vtec = 10.0
    else:
        baseline_vtec = 6.0

    storm_mult = 1.0 + max(0.0, (kp - 4.0) * 0.40)
    total_vtec = baseline_vtec * storm_mult

    # L-band range bias (1.3 GHz)
    f_l = 1.3e9
    range_bias_lband = (40.3e16 * total_vtec / f_l**2) * m

    coherence_degraded = s4 > 0.30

    return {
        "radar_range_bias_lband_m": round(range_bias_lband, 1),
        "radar_coherence_degraded": coherence_degraded,
        "radar_note": "L-band most affected. Scale by (1.3/f_GHz)² for other bands.",
    }


# ── Master risk computation ──────────────────────────────────────────────────


def compute_risk(
    lat: float,
    lon: float,
    kp: float | None = None,
    asset_type: str = DEFAULT_ASSET,
) -> dict:
    """
    Full operational risk assessment for a geographic point.

    Risk score (0–99) component breakdown:
      Kp component    (0–30): geomagnetic activity level
      S4 component    (0–25): scintillation severity
      HF component    (0–20): HF blackout probability
      GPS component   (0–20): GPS error relative to operational thresholds
      Bz boost        (0–10): southward IMF — storm precursor/enhancer

    Thresholds:
      NOMINAL   < 20:  all systems within normal parameters
      ELEVATED  20–39: measurable degradation; monitor and plan contingencies
      DEGRADED  40–59: significant impact; activate backup systems
      SEVERE    ≥ 60:  major storm; GPS unreliable for precision ops; HF likely blacked out

    GPS error thresholds used for component scaling:
      2 m (approx. calm baseline) → 0 pts
      25 m (C/A precision approach limit) → 20 pts
    """
    if kp is None:
        kp = get_kp()

    bz = get_bz()
    zone = lat_zone(lat)
    m = zone["multiplier"]

    # Sub-models
    s4 = compute_s4(lat, lon, kp)
    gps = compute_gps_error(lat, s4, kp, asset_type)
    hf = compute_hf_risk(lat, lon, kp)
    satcom = compute_satcom_risk(s4)
    radar = compute_radar_impact(kp, s4, lat)

    # Risk score
    kp_comp = min(30.0, kp * 3.33)
    s4_comp = min(25.0, s4 * 50.0)
    hf_comp = min(20.0, hf["hf_blackout_probability"] * 20.0)
    gps_m = gps["gps_error_m"]
    gps_comp = min(20.0, max(0.0, (gps_m - 2.0) / 23.0 * 20.0))
    # Southward Bz boosts score: −10 nT adds ~5 pts, −20 nT adds ~10 pts
    bz_boost = min(10.0, max(0.0, (-bz - 5.0) * 0.5)) if bz < -5.0 else 0.0

    score = min(99, round(kp_comp + s4_comp + hf_comp + gps_comp + bz_boost))

    if score < 20:
        level = "NOMINAL"
        rec = "All systems nominal. Standard operations authorized."
    elif score < 40:
        level = "ELEVATED"
        rec = (
            "GPS accuracy degraded. Monitor for escalation. "
            "Consider backup navigation for precision-dependent ops."
        )
    elif score < 60:
        level = "DEGRADED"
        rec = (
            "Significant ionospheric disturbance. Delay non-critical GPS-dependent ops. "
            "Activate backup comms. HF reliability reduced."
        )
    else:
        level = "SEVERE"
        rec = (
            "Major geomagnetic storm. GPS unreliable for precision navigation. "
            "HF likely blacked out. Postpone GPS-dependent operations. "
            "Use INS or backup navigation."
        )

    # Conditional operational watch notes
    watch_notes: list[str] = []
    if bz < -10.0:
        watch_notes.append(
            f"IMF Bz = {bz:.0f} nT (strongly southward) — "
            "active storm or onset likely within 1–2 h"
        )
    if kp >= 8:
        g_level = min(5, int(kp) - 4)
        watch_notes.append(f"G{g_level} geomagnetic storm in progress (Kp = {kp:.0f})")
    proton = get_proton_flux_10mev()
    if proton >= 100.0:
        s_idx = min(5, int(math.log10(proton / 10.0)) + 1)
        watch_notes.append(
            f"NOAA S{s_idx} solar energetic particle event "
            f"(proton flux ≥10 MeV = {proton:.0f} pfu) — "
            "polar cap HF blackout in progress"
        )
    if hf["pca_active"]:
        watch_notes.append(
            "Polar Cap Absorption active at this location — "
            "HF communications severely degraded poleward of ~65°"
        )

    return {
        "lat": lat,
        "lon": lon,
        "zone": zone["zone"],
        "zone_multiplier": m,
        "kp_current": round(kp, 1),
        "bz_current_nt": round(bz, 1),
        "solar_wind_km_s": round(get_wind_speed()),
        "assessment": {
            "risk_score": score,
            "risk_level": level,
            "recommendation": rec,
            "watch_notes": watch_notes,
            # GPS
            "gps_error_m": gps["gps_error_m"],
            "gps_error_range": gps["gps_error_range"],
            "vtec_estimate_tecu": gps["vtec_estimate_tecu"],
            "asset_type": gps["asset_type"],
            "iono_correction_active": gps["iono_correction_active"],
            # HF
            "hf_absorption_db": hf["hf_absorption_db"],
            "hf_sid_db": hf["hf_sid_db"],
            "hf_storm_db": hf["hf_storm_db"],
            "hf_pca_db": hf["hf_pca_db"],
            "hf_blackout_probability": hf["hf_blackout_probability"],
            "hf_dayside": hf["hf_dayside"],
            "pca_active": hf["pca_active"],
            "solar_zenith_deg": hf["solar_zenith_deg"],
            # SATCOM
            "satcom_fade_db": satcom["satcom_fade_db"],
            "satcom_outage_probability": satcom["satcom_outage_probability"],
            "satcom_applies_to": satcom["satcom_applies_to"],
            # Radar
            "radar_range_bias_lband_m": radar["radar_range_bias_lband_m"],
            "radar_coherence_degraded": radar["radar_coherence_degraded"],
            "radar_note": radar["radar_note"],
            # Scintillation
            "s4_index": s4,
        },
        "model_provenance": {
            "kp": "[MEASURED] NOAA SWPC planetary_k_index_1m",
            "bz": "[MEASURED] NOAA SWPC solar-wind/mag-2-hour (IMF Bz GSM)",
            "proton": "[MEASURED] NOAA GOES integral-protons-1-hour (≥10 MeV)",
            "s4": "[MODELED] Basu 1988 / Aquino 2005 (simplified lat-regime)",
            "gps": "[MODELED] VTEC L1 iono delay; Mannucci 2005 storm scaling",
            "hf": "[MODELED] CCIR-888 SID + auroral absorption + PCA (Bailey 1964)",
            "satcom": "[MODELED] ITU-R P.531-14 Nakagami fade (Ku/Ka GEO)",
            "radar": "[MODELED] L-band group delay from estimated VTEC",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data_age_seconds": data_age_seconds(),
    }
