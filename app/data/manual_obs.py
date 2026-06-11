"""
Operator-entered observations — the last-resort data path for disconnected ops.

Briefing-book Q6, third leg: when there is no live feed and no carried
cache (or the carried forecast has aged out), an operator can enter the Kp
value from whatever authoritative channel they do have — an S2 weather
section brief, a military space weather officer report, a radio broadcast.
The engine then runs the same doctrine rules against it.

Honesty contract:
  • Every output produced from a manual entry is labeled
    "operator-entered" with the operator's stated source and entry time.
  • Entries expire after MANUAL_OBS_TTL_SECONDS (default 3 h — one Kp bin):
    a stale manual value silently becoming "current" would be worse than
    no value.
  • Manual entries apply ONLY to mission assessment / equipment evaluation,
    never to the scientific telemetry endpoints — /api/status keeps showing
    what the feeds actually report.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

MANUAL_OBS_TTL_SECONDS = 3 * 3600  # one NOAA Kp bin


@dataclass
class ManualObservation:
    kp: float
    proton_flux_10mev_pfu: float | None
    xray_class: str | None  # A | B | C | M | X (operator briefs use class, not flux)
    source_note: str  # where the operator got it (free text, required)
    entered_at: str  # ISO UTC

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expires_at"] = self.expires_at()
        return d

    def expires_at(self) -> str:
        dt = datetime.fromisoformat(self.entered_at) + timedelta(seconds=MANUAL_OBS_TTL_SECONDS)
        return dt.isoformat()

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc).isoformat() >= self.expires_at()

    def xray_flux_wm2(self) -> float | None:
        """Map a flare class letter to its threshold flux (W/m²)."""
        return {"A": 1e-8, "B": 1e-7, "C": 1e-6, "M": 1e-5, "X": 1e-4}.get(
            (self.xray_class or "").strip().upper() or "?"
        )


_current: ManualObservation | None = None


def set_observation(
    kp: float,
    source_note: str,
    proton_flux_10mev_pfu: float | None = None,
    xray_class: str | None = None,
) -> ManualObservation:
    global _current
    _current = ManualObservation(
        kp=kp,
        proton_flux_10mev_pfu=proton_flux_10mev_pfu,
        xray_class=xray_class,
        source_note=source_note,
        entered_at=datetime.now(timezone.utc).isoformat(),
    )
    return _current


def get_observation() -> ManualObservation | None:
    """The active (non-expired) manual observation, or None."""
    if _current is None or _current.is_expired():
        return None
    return _current


def clear_observation() -> None:
    global _current
    _current = None


def manual_note(obs: ManualObservation) -> str:
    return (
        f"MANUAL — operator-entered Kp {obs.kp:g} "
        f"(source: {obs.source_note}, entered {obs.entered_at}, "
        f"expires {obs.expires_at()})."
    )
