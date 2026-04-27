"""Tests for app.models.impact — physics-grounded assertions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.impact import (
    GPS_ASSET_TYPES,
    NON_IONO_FLOOR_M,
    TEC_TO_L1_M_PER_TECU,
    assess_gps,
    assess_grid,
    assess_hf,
    assess_radar,
    assess_region,
    assess_satcom,
)
from app.models.ontology import FusedObservation, Region


def _obs(
    region: Region | None = None,
    *, kp=2.0, bz=0.0, wind=400.0, xray=1e-7,
    proton=0.1, f107=70.0, tec=15.0, anomaly=0.0,
    when: datetime | None = None,
) -> FusedObservation:
    return FusedObservation(
        region=region or Region.from_center(38, -98),
        when=when or datetime(2026, 4, 26, 18, 0, tzinfo=timezone.utc),
        kp_index=kp, bz_nt=bz, wind_speed_km_s=wind,
        xray_flux_wm2=xray, proton_flux_10mev_pfu=proton, f107_sfu=f107,
        tec_tecu=tec, tec_anomaly_tecu=anomaly, hmf2_km=300.0, nmf2=1.5e11,
    )


# ── GPS ──────────────────────────────────────────────────────────────────────


def test_gps_l1_quiet_conditions_low_error():
    """Quiet mid-lat → 3–5 m total (1.5 floor + ~2.5m iono)."""
    g = assess_gps(_obs(tec=10.0, anomaly=0.1))
    assert 3.0 <= g.error_m <= 6.0
    assert g.asset_type == "GPS_L1"
    assert not g.iono_correction_active


def test_gps_l1_storm_conditions_high_error():
    """G3 storm + elevated TEC → > 6 m."""
    g = assess_gps(_obs(kp=7.0, tec=30.0, anomaly=10.0))
    assert g.error_m > 6.0
    assert g.error_high_m > g.error_low_m


def test_gps_l1l2_much_lower_than_l1_at_storm():
    """Dual-frequency cancels first-order iono delay."""
    obs = _obs(kp=7.0, tec=30.0, anomaly=10.0)
    l1 = assess_gps(obs, "GPS_L1")
    l1l2 = assess_gps(obs, "GPS_L1L2")
    assert l1l2.error_m < l1.error_m
    assert l1l2.iono_correction_active


def test_gps_l1_klobuchar_relation():
    """Verify the 0.162 m/TECU coefficient appears in the math."""
    # tec=10, no scintillation → vertical delay = 1.62 m, *2.5 obliquity = 4.05
    # plus 1.5 floor → ~5.55, *1.0 factor → 5.55
    g = assess_gps(_obs(tec=10.0, anomaly=0.1))
    expected_low = NON_IONO_FLOOR_M + (TEC_TO_L1_M_PER_TECU * 10.0 * 2.5) - 0.5
    expected_high = NON_IONO_FLOOR_M + (TEC_TO_L1_M_PER_TECU * 10.0 * 2.5) + 0.5
    assert expected_low <= g.error_m <= expected_high


def test_gps_sbas_degrades_during_g3_storm():
    quiet = assess_gps(_obs(kp=2.0, tec=10.0, anomaly=0.1), "SBAS")
    storm = assess_gps(_obs(kp=7.0, tec=30.0, anomaly=8.0), "SBAS")
    assert storm.error_m > quiet.error_m


# ── HF ───────────────────────────────────────────────────────────────────────


def test_hf_quiet_low_absorption():
    """Quiet conditions → < 5 dB total absorption mid-lat."""
    hf = assess_hf(_obs())
    assert hf.absorption_total_db < 5.0
    assert hf.absorption_pca_db == 0.0
    assert not hf.pca_active


def test_hf_xclass_flare_dayside_drives_sid():
    """X1 flare overhead → 10–25 dB SID absorption."""
    # Dayside at lon=-98 (CONUS) at 18:00 UTC = ~12:00 local — sun overhead-ish
    hf = assess_hf(_obs(xray=2e-4))
    assert hf.absorption_sid_db > 10.0
    assert hf.is_dayside


def test_hf_xclass_flare_nightside_no_sid():
    """X1 flare at night-side longitude → 0 dB SID."""
    # 00:00 UTC at lon=0 → local solar time = midnight
    night_obs = _obs(
        region=Region.from_center(0, 0),
        when=datetime(2026, 4, 26, 0, 0, tzinfo=timezone.utc),
        xray=2e-4,
    )
    hf = assess_hf(night_obs)
    assert hf.absorption_sid_db < 1.0
    assert not hf.is_dayside


def test_hf_pca_only_polar():
    """S2 SEP (200 pfu) → PCA at polar regions only."""
    polar = assess_hf(_obs(region=Region.from_center(80, 0), proton=200.0))
    midlat = assess_hf(_obs(region=Region.from_center(38, -98), proton=200.0))
    assert polar.pca_active
    assert polar.absorption_pca_db > 5.0
    assert midlat.absorption_pca_db == 0.0


def test_hf_blackout_probability_caps_at_one():
    """Total absorption > 25 dB fade margin → blackout_prob = 1.0."""
    hf = assess_hf(_obs(
        region=Region.from_center(80, 0),
        xray=5e-4, proton=10000.0, kp=8.0,
    ))
    assert hf.blackout_probability == 1.0


# ── SATCOM ───────────────────────────────────────────────────────────────────


def test_satcom_l_quiet_no_fade():
    s = assess_satcom(_obs())
    assert s["L"].fade_db < 1.0
    assert s["L"].outage_probability == 0.0


def test_satcom_l_auroral_storm_fades():
    """Auroral region under G3 storm → meaningful L-band fade.

    At Kp=7 the S4 estimate is ~0.56; -10·log10(1 - 0.56²) ≈ 1.6 dB —
    physically correct moderate-scintillation fade.
    """
    obs = _obs(region=Region.from_center(60, 0), kp=7.0)
    s = assess_satcom(obs)
    assert s["L"].fade_db > 1.0
    assert s["L"].s4_used > 0.4


def test_satcom_ku_unaffected_by_scint():
    """Ku-band is scint-immune in our model — always 0 fade."""
    obs = _obs(region=Region.from_center(60, 0), kp=8.0)
    s = assess_satcom(obs)
    assert s["Ku"].fade_db == 0.0
    assert s["Ku"].outage_probability == 0.0


# ── Radar ────────────────────────────────────────────────────────────────────


def test_radar_range_bias_scales_inversely_with_freq_squared():
    """Skolnik: bias ∝ 1/f². X-band should be ~(1.3/10)² ≈ 1.7% of L-band."""
    r = assess_radar(_obs(tec=15.0))
    ratio = r["X"].range_bias_m / r["L"].range_bias_m
    expected = (1.3 / 10.0) ** 2
    assert abs(ratio - expected) < 0.01


def test_radar_range_bias_known_value_lband():
    """40.3 × 10^16 × 15 / (1.3e9)² ≈ 3.58 m at 15 TECu."""
    r = assess_radar(_obs(tec=15.0))
    expected = 40.3e16 * 15.0 / (1.3e9 ** 2)
    assert abs(r["L"].range_bias_m - expected) < 0.05


def test_radar_coherence_loss_at_high_s4():
    """Auroral G2 storm → S4 > 0.30 → coherence flagged."""
    obs = _obs(region=Region.from_center(60, 0), kp=6.0)
    r = assess_radar(obs)
    assert r["L"].coherence_degraded


# ── Region-level ─────────────────────────────────────────────────────────────


def test_assess_region_returns_full_tree():
    a = assess_region(_obs())
    assert set(a.gps.keys()) == set(GPS_ASSET_TYPES)
    assert {"L", "Ku"} == set(a.satcom.keys())
    assert {"L", "S", "C", "X"} == set(a.radar.keys())


def test_to_rows_flattens_and_carries_region_id():
    a = assess_region(_obs())
    rows = a.to_rows()
    # 5 GPS + 1 HF + 2 SATCOM + 4 RADAR = 12 rows
    assert len(rows) == 12
    for r in rows:
        assert r["region_id"] == a.region.region_id
        assert "system" in r
        assert "metric" in r
        assert "value" in r


def test_assess_grid_runs_over_full_global_mesh():
    from app.models.ontology import global_grid
    grid = global_grid()
    obs_list = [
        FusedObservation(
            region=r,
            when=datetime(2026, 4, 26, 18, 0, tzinfo=timezone.utc),
            kp_index=3.7, bz_nt=-2.0, wind_speed_km_s=420.0,
            xray_flux_wm2=1e-6, proton_flux_10mev_pfu=0.5, f107_sfu=120.0,
            tec_tecu=18.0, tec_anomaly_tecu=2.0, hmf2_km=310.0, nmf2=2.5e11,
        )
        for r in grid
    ]
    impacts = assess_grid(obs_list)
    assert len(impacts) == 324
    # Total flat rows: 12 per region
    flat = [row for ia in impacts for row in ia.to_rows()]
    assert len(flat) == 324 * 12
