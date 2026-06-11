"""
Replay scenarios — real recorded storm conditions for demos and training.

WarHacker P0-3. The 90-second demo needs storm conditions on command, and
live space weather won't cooperate on schedule. The honest way to do that
is NOT to fabricate a Kp value — it's to replay the measured conditions of
a real, documented storm, clearly labeled as a replay everywhere it
surfaces (data quality notes, provenance, UI banner).

Values below are the published peak measurements of the May 10–11, 2024
"Gannon" storm (G5 — the strongest geomagnetic storm since 2003):
  • Kp 9.0           — NOAA/GFZ planetary index, May 10–11, 2024
  • Bz ≈ −50 nT      — DSCOVR solar wind magnetometer, southward excursions
  • X5.8 flare       — GOES-16 XRS, May 11, 2024 (5.8e-4 W/m²)
  • ~208 pfu protons — GOES ≥10 MeV integral flux (S2 radiation storm)
  • ~750 km/s wind   — DSCOVR Faraday cup, CME-driven solar wind

The same storm is also used by the validation story: the engine replays a
known event and its outputs can be checked against documented impacts
(GPS degradation, HF blackouts, airline reroutes).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplayScenario:
    """A documented historical event, replayable through the engine."""

    id: str
    title: str
    occurred: str  # human-readable date of the real event
    kp: float
    bz_nt: float
    xray_flux_wm2: float
    proton_flux_10mev_pfu: float
    wind_speed_km_s: float
    citation: str
    # Recorded 3-hour Kp sequence: (hour offset from event-day 00:00 UTC, Kp).
    # Used by the time-windowed ATAK overlay — the replay maps this sequence
    # onto the demo day, hour-for-hour. Values must be published measurements.
    kp_timeline: tuple[tuple[int, float], ...] = ()


REPLAY_SCENARIOS: dict[str, ReplayScenario] = {
    "gannon-2024": ReplayScenario(
        id="gannon-2024",
        title="May 2024 Gannon storm (G5) — peak measured conditions",
        occurred="2024-05-10 to 2024-05-11 UTC",
        kp=9.0,
        bz_nt=-50.0,
        xray_flux_wm2=5.8e-4,  # X5.8
        proton_flux_10mev_pfu=208.0,  # S2
        wind_speed_km_s=750.0,
        citation=("NOAA SWPC G5 event reports, May 2024 (Gannon storm); " "GOES-16 XRS + DSCOVR measurements"),
        # GFZ Potsdam definitive Kp, 2024-05-10 00:00 → 2024-05-12 00:00 UTC
        # (kp.gfz.de, CC BY 4.0). Quiet morning, storm onset in the 15-18 UTC
        # bin, G5 overnight — the recorded shape drives the overlay windows.
        kp_timeline=(
            (0, 2.667),
            (3, 2.667),
            (6, 2.333),
            (9, 2.0),
            (12, 3.667),
            (15, 7.667),
            (18, 8.667),
            (21, 8.667),
            (24, 9.0),
            (27, 8.333),
            (30, 8.333),
            (33, 9.0),
            (36, 8.667),
            (39, 8.333),
            (42, 7.667),
            (45, 7.667),
        ),
    ),
}

REPLAY_NOTE = "REPLAY — historical storm conditions ({title}), measured values, not live data."


def replay_note(scn: ReplayScenario) -> str:
    return REPLAY_NOTE.format(title=scn.title)
