"""
IonShield ontology — formal data model for the fusion + impact pipeline.

These dataclasses define the canonical shapes that flow through the system
and that Foundry Object Types should mirror. They are intentionally:

- Immutable (`frozen=True`) where they describe a snapshot of state, so the
  same instance can be safely shared between threads and serialized stably.
- Plain data — no business logic. Logic lives in vulnerability.py / fusion.py.
- JSON-serializable via `dataclasses.asdict` so they can be pushed straight
  to a Foundry dataset row.

The shapes here are the contract between A1 (ingestion) and A4 (impact
modeling): a fused observation at (region, time) is the atomic unit of work
the impact models operate on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


# ── Dimensions ────────────────────────────────────────────────────────────────


class EventType(str, Enum):
    """Categorical space-weather event labels.

    Mirrors NOAA scales where applicable (G, S, R) plus phase-resolved
    geomagnetic storm states for replay/analytics.
    """

    BACKGROUND = "BACKGROUND"  # quiet conditions
    GEOMAG_INITIAL = "GEOMAG_INITIAL"  # storm sudden commencement
    GEOMAG_MAIN = "GEOMAG_MAIN"  # main phase, Dst dropping
    GEOMAG_RECOVERY = "GEOMAG_RECOVERY"  # recovery phase
    SUBSTORM = "SUBSTORM"  # auroral substorm
    SEP_EVENT = "SEP_EVENT"  # solar energetic particle event (S-scale)
    FLARE_M = "FLARE_M"  # M-class X-ray flare (R1–R2)
    FLARE_X = "FLARE_X"  # X-class X-ray flare (R3+)
    PCA = "PCA"  # polar cap absorption


class SystemType(str, Enum):
    """Operational systems IonShield assesses.

    The set is closed; new systems require an entry in VULNERABILITY_MATRIX.
    """

    GPS_L1 = "GPS_L1"  # single-frequency L1 C/A
    GPS_L1L2 = "GPS_L1L2"  # dual-frequency, ionospheric correction
    GPS_L1L5 = "GPS_L1L5"  # modernized dual-frequency
    GPS_INS = "GPS_INS"  # INS-aided GPS (degraded gracefully)
    SBAS = "SBAS"  # WAAS/EGNOS-corrected
    HF_RADIO = "HF_RADIO"  # 3–30 MHz skywave
    SATCOM_L = "SATCOM_L"  # L-band satellite (1–2 GHz, scintillation-prone)
    SATCOM_KU = "SATCOM_KU"  # Ku-band satellite (12–18 GHz, less affected)
    RADAR_HF = "RADAR_HF"  # OTH HF radar
    RADAR_VHF = "RADAR_VHF"  # VHF surveillance radar


class OperationalState(str, Enum):
    """Coarse operational state for a system in a region.

    Ordered from best to worst: comparisons via `value` ordering are not safe
    in Python Enums by default — use `OPERATIONAL_STATE_ORDER` for ranking.
    """

    NOMINAL = "NOMINAL"
    ELEVATED = "ELEVATED"
    DEGRADED = "DEGRADED"
    SEVERE = "SEVERE"


OPERATIONAL_STATE_ORDER: dict[OperationalState, int] = {
    OperationalState.NOMINAL: 0,
    OperationalState.ELEVATED: 1,
    OperationalState.DEGRADED: 2,
    OperationalState.SEVERE: 3,
}


# ── Region grid ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Region:
    """
    A grid cell in the global lat/lon mesh.

    `region_id` is the canonical key: `"R{lat:+04.0f}{lon:+04.0f}"`, e.g.
    `"R+30+150"` for a 10°×20° cell centered at (30°N, 150°E). The id is
    stable across runs so it can be used as a primary key in Foundry.
    """

    region_id: str
    lat_deg: float  # cell center
    lon_deg: float  # cell center
    lat_size_deg: float  # cell height
    lon_size_deg: float  # cell width
    geomag_lat_deg: float  # geomagnetic latitude (centered dipole approx)

    @classmethod
    def from_center(
        cls,
        lat: float,
        lon: float,
        lat_size: float = 10.0,
        lon_size: float = 20.0,
    ) -> "Region":
        rid = f"R{int(round(lat)):+04d}{int(round(lon)):+04d}"
        return cls(
            region_id=rid,
            lat_deg=lat,
            lon_deg=lon,
            lat_size_deg=lat_size,
            lon_size_deg=lon_size,
            geomag_lat_deg=geomagnetic_latitude(lat, lon),
        )

    @property
    def is_polar(self) -> bool:
        """|geomag lat| ≥ 60° — polar cap absorption regime when SEPs occur."""
        return abs(self.geomag_lat_deg) >= 60.0

    @property
    def is_auroral(self) -> bool:
        """50° ≤ |geomag lat| < 70° — auroral oval, scintillation-prone."""
        return 50.0 <= abs(self.geomag_lat_deg) < 70.0

    @property
    def is_equatorial(self) -> bool:
        """|geomag lat| ≤ 20° — equatorial anomaly, post-sunset bubbles."""
        return abs(self.geomag_lat_deg) <= 20.0


def geomagnetic_latitude(lat_deg: float, lon_deg: float) -> float:
    """
    Geomagnetic latitude under a centered dipole approximation.

    The IGRF magnetic pole sits near (80.65°N, 287.32°E) in 2025. This is a
    rough but adequate approximation for dividing the globe into auroral /
    polar / mid-latitude regimes — accurate to within a few degrees at the
    latitudes that matter for HF / scintillation thresholds.

    For sub-degree precision, swap in pyIGRF; for v1 we accept the error.
    """
    import math

    pole_lat = math.radians(80.65)
    pole_lon = math.radians(287.32)
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_mag = math.sin(pole_lat) * math.sin(lat) + math.cos(pole_lat) * math.cos(lat) * math.cos(lon - pole_lon)
    sin_mag = max(-1.0, min(1.0, sin_mag))
    return math.degrees(math.asin(sin_mag))


def global_grid(lat_size: float = 10.0, lon_size: float = 20.0) -> list[Region]:
    """Generate the full global grid at the given cell resolution."""
    out: list[Region] = []
    lat = -90.0 + lat_size / 2
    while lat < 90.0:
        lon = -180.0 + lon_size / 2
        while lon < 180.0:
            out.append(Region.from_center(lat, lon, lat_size, lon_size))
            lon += lon_size
        lat += lat_size
    return out


# ── Time + observation ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimeWindow:
    """
    A half-open time interval [start, end). All times UTC.

    cadence_seconds documents the producer cadence (e.g. 600 for GloTEC's
    10-minute updates) so consumers know how dense the underlying samples
    are inside the window.
    """

    start: datetime
    end: datetime
    cadence_seconds: int = 0

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("TimeWindow end must be after start")

    @classmethod
    def at(cls, t: datetime, cadence_seconds: int = 0) -> "TimeWindow":
        """An instantaneous window of width 1µs at t (handy for snapshots)."""
        from datetime import timedelta

        return cls(t, t + timedelta(microseconds=1), cadence_seconds)


@dataclass(frozen=True)
class FusedObservation:
    """
    The atomic unit of fused state at (region, time).

    Carries everything the impact models need: solar drivers (scalar, broadcast
    from NOAA), local ionospheric state (from GloTEC), geomagnetic indices,
    plus the data-quality bookkeeping needed for confidence scoring.
    """

    region: Region
    when: datetime

    # Global solar drivers (scalar, broadcast)
    kp_index: float
    bz_nt: float
    wind_speed_km_s: float
    xray_flux_wm2: float
    proton_flux_10mev_pfu: float
    f107_sfu: float

    # Local ionospheric state (from GloTEC interpolation)
    tec_tecu: float
    tec_anomaly_tecu: float
    hmf2_km: float
    nmf2: float

    # Bookkeeping
    data_age_seconds: int = 0
    feed_quality: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Foundry-row-shaped flat dict (no nested objects)."""
        return {
            "region_id": self.region.region_id,
            "lat_deg": self.region.lat_deg,
            "lon_deg": self.region.lon_deg,
            "geomag_lat_deg": self.region.geomag_lat_deg,
            "when_utc": self.when.astimezone(timezone.utc).isoformat(),
            "kp_index": self.kp_index,
            "bz_nt": self.bz_nt,
            "wind_speed_km_s": self.wind_speed_km_s,
            "xray_flux_wm2": self.xray_flux_wm2,
            "proton_flux_10mev_pfu": self.proton_flux_10mev_pfu,
            "f107_sfu": self.f107_sfu,
            "tec_tecu": self.tec_tecu,
            "tec_anomaly_tecu": self.tec_anomaly_tecu,
            "hmf2_km": self.hmf2_km,
            "nmf2": self.nmf2,
            "data_age_seconds": self.data_age_seconds,
            "feed_quality": self.feed_quality,
        }


# ── Vulnerability dimensions ─────────────────────────────────────────────────


class Driver(str, Enum):
    """Environmental drivers a SystemType can be vulnerable to."""

    KP = "KP"
    BZ = "BZ"
    WIND_SPEED = "WIND_SPEED"
    XRAY_FLUX = "XRAY_FLUX"
    PROTON_FLUX = "PROTON_FLUX"
    F107 = "F107"
    TEC = "TEC"
    TEC_ANOMALY = "TEC_ANOMALY"


@dataclass(frozen=True)
class OperationalThreshold:
    """
    A `driver crosses value → state` rule.

    The rule fires when, for the given region, the named driver in a
    FusedObservation crosses `value` in the direction implied by `comparator`.
    A SystemType degrades to the WORST state among all fired rules.

    `region_filter` (optional) restricts the rule to a region predicate
    (e.g. "polar" → fires only when region.is_polar). None = global.
    """

    driver: Driver
    comparator: str  # ">=" | ">" | "<=" | "<"
    value: float
    state: OperationalState
    region_filter: str | None = None  # None | "polar" | "auroral" | "equatorial"
    rationale: str = ""

    def fires(self, env_value: float, region: Region) -> bool:
        if self.region_filter == "polar" and not region.is_polar:
            return False
        if self.region_filter == "auroral" and not region.is_auroral:
            return False
        if self.region_filter == "equatorial" and not region.is_equatorial:
            return False
        if self.comparator == ">=":
            return env_value >= self.value
        if self.comparator == ">":
            return env_value > self.value
        if self.comparator == "<=":
            return env_value <= self.value
        if self.comparator == "<":
            return env_value < self.value
        raise ValueError(f"Unknown comparator: {self.comparator}")


@dataclass(frozen=True)
class Vulnerability:
    """
    A SystemType + ordered list of OperationalThresholds.

    Rules are evaluated independently; the resulting state is the worst that
    fires. Use `evaluate(...)` to score a FusedObservation.
    """

    system: SystemType
    thresholds: tuple[OperationalThreshold, ...]
