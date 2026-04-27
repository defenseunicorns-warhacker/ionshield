"""Tests for app.models.ontology — Region, geomag latitude, grid generation."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from app.models.ontology import (
    Driver,
    EventType,
    FusedObservation,
    OperationalState,
    OperationalThreshold,
    OPERATIONAL_STATE_ORDER,
    Region,
    SystemType,
    TimeWindow,
    geomagnetic_latitude,
    global_grid,
)


def test_region_id_is_stable_and_descriptive():
    r = Region.from_center(30, 150)
    assert r.region_id == "R+030+150"
    r2 = Region.from_center(-45, -90)
    assert r2.region_id == "R-045-090"


def test_region_zones():
    # CONUS midlat: not polar / not auroral / not equatorial
    r_conus = Region.from_center(38, -98)
    assert not r_conus.is_polar
    assert not r_conus.is_equatorial

    # Geographic pole: polar
    r_pole = Region.from_center(85, 0)
    assert r_pole.is_polar

    # Equator: equatorial
    r_eq = Region.from_center(0, 0)
    assert r_eq.is_equatorial


def test_geomag_latitude_known_points():
    # North geographic pole sits ~9° from the magnetic pole, so its geomag
    # latitude is roughly 80–82°.
    g = geomagnetic_latitude(90, 0)
    assert 78 < g < 90
    # Equator at the magnetic pole longitude is well above geographic equator.
    assert geomagnetic_latitude(0, 287.32) > 0
    # Equator opposite the pole is well south.
    assert geomagnetic_latitude(0, 107.32) < 0


def test_global_grid_size():
    g = global_grid(lat_size=10, lon_size=20)
    assert len(g) == 18 * 18
    # First cell should sit at southwest corner
    assert g[0].lat_deg == -85
    assert g[0].lon_deg == -170


def test_time_window_validation():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        TimeWindow(now, now)
    tw = TimeWindow(now, now + timedelta(minutes=10), cadence_seconds=600)
    assert tw.cadence_seconds == 600


def test_operational_threshold_fires():
    rule = OperationalThreshold(Driver.KP, ">=", 5, OperationalState.ELEVATED)
    r = Region.from_center(0, 0)
    assert rule.fires(5.0, r)
    assert rule.fires(7.0, r)
    assert not rule.fires(4.9, r)


def test_polar_filter_only_fires_polar():
    rule = OperationalThreshold(
        Driver.PROTON_FLUX, ">=", 10, OperationalState.ELEVATED, region_filter="polar"
    )
    r_polar = Region.from_center(85, 0)
    r_eq = Region.from_center(0, 0)
    assert rule.fires(50.0, r_polar)
    assert not rule.fires(50.0, r_eq)


def test_fused_observation_to_dict_is_flat():
    r = Region.from_center(38, -98)
    obs = FusedObservation(
        region=r,
        when=datetime(2026, 4, 26, tzinfo=timezone.utc),
        kp_index=3.7, bz_nt=-2.0, wind_speed_km_s=420.0,
        xray_flux_wm2=1e-6, proton_flux_10mev_pfu=0.5, f107_sfu=120.0,
        tec_tecu=18.0, tec_anomaly_tecu=2.0, hmf2_km=310.0, nmf2=2.5e11,
    )
    d = obs.to_dict()
    assert d["region_id"] == r.region_id
    assert d["lat_deg"] == 38
    assert d["when_utc"].endswith("+00:00")
    # No nested dataclass refs leak into the row
    assert "region" not in d


def test_state_order_is_total():
    states = list(OPERATIONAL_STATE_ORDER.keys())
    ranks = [OPERATIONAL_STATE_ORDER[s] for s in states]
    assert ranks == sorted(ranks)
    assert OPERATIONAL_STATE_ORDER[OperationalState.SEVERE] > OPERATIONAL_STATE_ORDER[OperationalState.ELEVATED]


def test_event_type_enum_values_stable():
    assert EventType.GEOMAG_MAIN.value == "GEOMAG_MAIN"
    assert SystemType.GPS_L1.value == "GPS_L1"
