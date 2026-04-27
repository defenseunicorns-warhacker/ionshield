"""
Lightweight in-process latency instrumentation.

Maintains a rolling window of recent fetch durations per data source, plus
per-stage timings (fetch / archive / sync / detect) for the refresh loop.
Surfaced via /api/v3/health so an operator can see at a glance:

  - Which feed is slow this minute (P50 / P95 latency)
  - Which loop stage is dominating wall-clock per tick
  - Whether the loop is keeping up with its target cadence

No external metrics library; for production-scale deployment this would
front-end Prometheus / OpenTelemetry. Same data, more sinks.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

_WINDOW = 50  # samples per source/stage


class _Hist:
    """Tiny rolling histogram. P50/P95 via sort on snapshot — fine at N≤50."""

    def __init__(self, window: int = _WINDOW) -> None:
        self._d: deque[float] = deque(maxlen=window)

    def add(self, ms: float) -> None:
        self._d.append(ms)

    def snapshot(self) -> dict:
        if not self._d:
            return {"n": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
        s = sorted(self._d)
        n = len(s)
        return {
            "n": n,
            "p50_ms": round(s[n // 2], 1),
            "p95_ms": round(s[max(0, int(n * 0.95) - 1)], 1),
            "max_ms": round(s[-1], 1),
        }


# Per-source and per-stage histograms
_sources: dict[str, _Hist] = {}
_stages: dict[str, _Hist] = {}
_last_loop_start: float | None = None
_loop_durations: _Hist = _Hist()


def _h(d: dict[str, _Hist], k: str) -> _Hist:
    h = d.get(k)
    if h is None:
        h = _Hist()
        d[k] = h
    return h


@asynccontextmanager
async def time_source(name: str):
    """Time an async source fetch; result goes into the per-source histogram."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000
        _h(_sources, name).add(ms)


@contextmanager
def time_stage(name: str) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000
        _h(_stages, name).add(ms)


def begin_loop_tick() -> None:
    global _last_loop_start
    if _last_loop_start is not None:
        gap = (time.perf_counter() - _last_loop_start) * 1000
        _loop_durations.add(gap)
    _last_loop_start = time.perf_counter()


def snapshot() -> dict:
    """Build a JSON-serializable snapshot for the /health endpoint."""
    return {
        "sources": {k: v.snapshot() for k, v in _sources.items()},
        "stages": {k: v.snapshot() for k, v in _stages.items()},
        "loop_interval": _loop_durations.snapshot(),
    }


def reset() -> None:
    """Test helper — clears all in-memory histograms."""
    global _last_loop_start
    _sources.clear()
    _stages.clear()
    _loop_durations._d.clear()
    _last_loop_start = None
