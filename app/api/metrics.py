"""
Prometheus exposition for IonShield internals — pure stdlib generator.

We deliberately do NOT depend on `prometheus_client` so the Render bundle
stays small. The exposition format is small and well-defined; this module
emits text directly from the in-process histograms / breaker state.

Scrapers should hit `GET /metrics` (no API key required by default — adjust
in routes.py if your deployment exposes it externally).

Metrics emitted:

  ionshield_source_fetch_duration_ms{source="..."}   summary p50/p95/max
  ionshield_source_fetch_count{source="..."}         counter
  ionshield_stage_duration_ms{stage="..."}           summary p50/p95/max
  ionshield_loop_interval_ms                         summary p50/p95/max
  ionshield_breaker_state{source="...",state="..."}  gauge (0/1)
  ionshield_breaker_failures_total{source="..."}     counter
  ionshield_breaker_successes_total{source="..."}    counter
"""

from __future__ import annotations

from app.data.circuit_breaker import BreakerState
from app.data.instrumentation import snapshot as instr_snapshot
from app.data.registry import list_sources


def _line(name: str, labels: dict[str, str], value: float | int | None) -> str:
    if value is None:
        return ""
    if labels:
        rendered = ",".join(f'{k}="{v}"' for k, v in labels.items())
        return f"{name}{{{rendered}}} {value}"
    return f"{name} {value}"


def _summary_from_hist(name: str, hist: dict, labels: dict[str, str]) -> list[str]:
    """Emit p50/p95/max + count for a rolling histogram."""
    n = hist.get("n") or 0
    if n == 0:
        return []
    out = [
        _line(name, {**labels, "quantile": "0.5"}, hist.get("p50_ms")),
        _line(name, {**labels, "quantile": "0.95"}, hist.get("p95_ms")),
        _line(name, {**labels, "quantile": "1.0"}, hist.get("max_ms")),
    ]
    out.append(_line(f"{name}_count", labels, n))
    return [line for line in out if line]


def render() -> str:
    lines: list[str] = []

    inst = instr_snapshot()

    # ── Source fetch durations
    lines.append("# HELP ionshield_source_fetch_duration_ms Per-source fetch latency")
    lines.append("# TYPE ionshield_source_fetch_duration_ms summary")
    for src, hist in inst["sources"].items():
        lines.extend(_summary_from_hist(
            "ionshield_source_fetch_duration_ms", hist, {"source": src},
        ))

    # ── Refresh-loop stage durations
    lines.append("# HELP ionshield_stage_duration_ms Refresh-loop stage latency")
    lines.append("# TYPE ionshield_stage_duration_ms summary")
    for stage, hist in inst["stages"].items():
        lines.extend(_summary_from_hist(
            "ionshield_stage_duration_ms", hist, {"stage": stage},
        ))

    # ── Loop interval
    li = inst["loop_interval"]
    if li.get("n", 0) > 0:
        lines.append("# HELP ionshield_loop_interval_ms Wall time between refresh loop ticks")
        lines.append("# TYPE ionshield_loop_interval_ms summary")
        lines.extend(_summary_from_hist(
            "ionshield_loop_interval_ms", li, {},
        ))

    # ── Breaker state + counters
    lines.append("# HELP ionshield_breaker_state Current breaker state (1 if matching state, else 0)")
    lines.append("# TYPE ionshield_breaker_state gauge")
    lines.append("# HELP ionshield_breaker_failures_total Total recorded failures per source")
    lines.append("# TYPE ionshield_breaker_failures_total counter")
    lines.append("# HELP ionshield_breaker_successes_total Total recorded successes per source")
    lines.append("# TYPE ionshield_breaker_successes_total counter")
    for src in list_sources():
        snap = src.breaker.snapshot()
        for st in BreakerState:
            lines.append(_line(
                "ionshield_breaker_state",
                {"source": src.name, "state": st.value},
                1 if snap["state"] == st.value else 0,
            ))
        lines.append(_line(
            "ionshield_breaker_failures_total",
            {"source": src.name},
            snap["total_failures"],
        ))
        lines.append(_line(
            "ionshield_breaker_successes_total",
            {"source": src.name},
            snap["total_successes"],
        ))

    return "\n".join(line for line in lines if line) + "\n"
