"""Tests for app.data.fusion — Region × scalar fusion with GloTEC overlay."""

from __future__ import annotations

from datetime import datetime, timezone

from app.data.fusion import _glotec_at, _index_glotec, fuse_snapshot, fused_grid_payload
from app.models.ontology import Region


def _fc(points: list[tuple[float, float, float, float]]) -> dict:
    """Build a minimal GloTEC FeatureCollection from (lat, lon, tec, anomaly)."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "tec": tec, "anomaly": anom, "hmF2": 300.0,
                    "NmF2": 1.5e11, "quality_flag": 0,
                },
            }
            for (lat, lon, tec, anom) in points
        ],
    }


def test_index_glotec_buckets_points():
    fc = _fc([(30, 150, 12.0, 1.0), (-30, -90, 8.0, -0.5)])
    idx = _index_glotec(fc)
    assert (6, 30) in idx
    assert (-6, -18) in idx


def test_glotec_at_returns_climatology_when_empty():
    """Empty index → storm-aware climatology. At kp=0, lat=30 mid-lat → 10 TECu."""
    out = _glotec_at({}, 30, 150, kp=0.0)
    assert out["tec"] == 10.0
    assert out["hmf2"] == 300.0


def test_glotec_at_climatology_storm_enhanced():
    """At kp=7, mid-lat baseline 10.0 × (1 + 0.40·3) = 22.0 TECu."""
    out = _glotec_at({}, 30, 150, kp=7.0)
    assert out["tec"] == 10.0 * (1 + 0.40 * 3)


def test_glotec_at_returns_equatorial_climatology_at_low_lat():
    """|lat|<20 → 15 TECu baseline."""
    out = _glotec_at({}, 5, 0, kp=0.0)
    assert out["tec"] == 15.0


def test_glotec_at_finds_nearest_bucket():
    fc = _fc([(30, 150, 12.0, 1.0)])
    idx = _index_glotec(fc)
    # Slightly offset from the source point — should still resolve
    out = _glotec_at(idx, 32, 152)
    assert out["tec"] == 12.0


def test_glotec_skips_bad_quality():
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [150, 30]},
                "properties": {"tec": 99.0, "quality_flag": 2},
            },
        ],
    }
    idx = _index_glotec(fc)
    out = _glotec_at(idx, 30, 150, kp=0.0)
    # Bad quality → falls through to storm-aware climatology (lat=30, kp=0 → 10)
    assert out["tec"] == 10.0


def test_fuse_snapshot_full_grid_with_real_shape():
    fc = _fc([
        (30, 150, 22.0, 5.0),
        (-30, -90, 9.0, -1.0),
        (60, 0, 18.0, 3.0),
    ])
    fused = fuse_snapshot(
        when=datetime(2026, 4, 26, tzinfo=timezone.utc),
        kp=4.0, bz_nt=-3.0, wind_speed_km_s=480.0,
        xray_flux_wm2=2e-6, proton_flux_10mev_pfu=0.5, f107_sfu=120.0,
        glotec_fc=fc,
        feed_quality={"kp": "ok"},
        data_age_seconds=60,
    )
    # Default 10x20 grid → 18*18 cells
    assert len(fused) == 324
    # Every observation carries the broadcast scalars
    for obs in fused:
        assert obs.kp_index == 4.0
        assert obs.bz_nt == -3.0
        assert obs.f107_sfu == 120.0
    # The cell nearest (30, 150) should pick up the 22 TECu point
    near = next(o for o in fused if abs(o.region.lat_deg - 30) <= 5 and abs(o.region.lon_deg - 150) <= 10)
    assert near.tec_tecu == 22.0
    assert near.tec_anomaly_tecu == 5.0


def test_fuse_snapshot_falls_back_when_glotec_missing():
    """Without GloTEC, fall back to lat-bucketed quiet climatology at low Kp."""
    fused = fuse_snapshot(
        when=datetime(2026, 4, 26, tzinfo=timezone.utc),
        kp=2.0, bz_nt=0.0, wind_speed_km_s=400.0,
        xray_flux_wm2=1e-7, proton_flux_10mev_pfu=0.1, f107_sfu=70.0,
        glotec_fc=None,
    )
    assert len(fused) == 324
    # Spot-check zones at low Kp: equatorial=15, mid=10, polar=6
    eq = next(o for o in fused if abs(o.region.lat_deg) < 20)
    midlat = next(o for o in fused if 20 < abs(o.region.lat_deg) < 55)
    polar = next(o for o in fused if abs(o.region.lat_deg) > 60)
    assert eq.tec_tecu == 15.0
    assert midlat.tec_tecu == 10.0
    assert polar.tec_tecu == 6.0
    for obs in fused:
        assert obs.hmf2_km == 300.0


def test_fuse_snapshot_storm_enhances_climatology_tec():
    """At Kp=7 without GloTEC, climatology TEC scales by (1 + 0.40·(Kp-4))."""
    fused = fuse_snapshot(
        when=datetime(2026, 4, 26, tzinfo=timezone.utc),
        kp=7.0, bz_nt=-10.0, wind_speed_km_s=600.0,
        xray_flux_wm2=1e-6, proton_flux_10mev_pfu=1.0, f107_sfu=120.0,
        glotec_fc=None,
    )
    storm_mult = 1.0 + 0.40 * (7.0 - 4.0)  # = 2.2
    midlat = next(o for o in fused if 20 < abs(o.region.lat_deg) < 55)
    assert abs(midlat.tec_tecu - 10.0 * storm_mult) < 1e-9
    polar = next(o for o in fused if abs(o.region.lat_deg) > 60)
    assert abs(polar.tec_tecu - 6.0 * storm_mult) < 1e-9


def test_fuse_snapshot_quiet_kp_no_storm_enhancement():
    """Kp ≤ 4 must not enhance the climatology baseline."""
    fused = fuse_snapshot(
        when=datetime(2026, 4, 26, tzinfo=timezone.utc),
        kp=3.5, bz_nt=0.0, wind_speed_km_s=400.0,
        xray_flux_wm2=1e-7, proton_flux_10mev_pfu=0.1, f107_sfu=70.0,
        glotec_fc=None,
    )
    midlat = next(o for o in fused if 20 < abs(o.region.lat_deg) < 55)
    assert midlat.tec_tecu == 10.0


def test_fused_grid_payload_shape():
    fused = fuse_snapshot(
        when=None,
        kp=2.0, bz_nt=0.0, wind_speed_km_s=400.0,
        xray_flux_wm2=1e-7, proton_flux_10mev_pfu=0.1, f107_sfu=70.0,
        glotec_fc=None,
    )
    payload = fused_grid_payload(fused)
    assert payload["n_regions"] == 324
    assert "rows" in payload
    assert payload["rows"][0]["region_id"].startswith("R")
