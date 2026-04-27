"""
Event persistence + detection-loop integration.

Glue between:
  - the rule-based detector in app.models.events (pure functions)
  - the SQL `events` table in app.data.db (durable state)
  - app.data.foundry_sync.sync_rows (analytical mirror)

`detect_and_persist(obs, window)` is the single entry point the refresh loop
calls. It opens a transaction, looks up open events per rule, runs the rule
evaluator, and either inserts or updates rows. Newly-inserted ONSET events
and freshly-ENDED events are returned so the caller can sync them to
Foundry — PEAK updates are not synced individually to avoid row-amplifying
the Foundry dataset.
"""

from __future__ import annotations

import logging

from sqlalchemy import insert, select, update

from app.data.db import events as events_table
from app.data.db import get_engine
from app.data import feedback_store
from app.models.events import (
    RULES,
    DetectionResult,
    Event,
    EventState,
    MLClassifierStub,
    evaluate_rule,
)
from app.models.ml_classifier import (
    TrainedClassifier,
    featurize,
    get_challenger,
)
from app.models.ontology import Driver, EventType, FusedObservation

logger = logging.getLogger(__name__)


async def _open_event_for_rule(conn, event_type: EventType, region_id: str = "GLOBAL"):
    """Most-recent non-ENDED row for (event_type, region_id), or None."""
    stmt = (
        select(events_table)
        .where(events_table.c.event_type == event_type.value)
        .where(events_table.c.region_id == region_id)
        .where(events_table.c.state != EventState.ENDED.value)
        .order_by(events_table.c.t_onset.desc())
        .limit(1)
    )
    result = await conn.execute(stmt)
    return result.mappings().first()


def _row_to_event(row) -> Event:
    return Event(
        event_type=EventType(row["event_type"]),
        state=EventState(row["state"]),
        severity=row["severity"],
        region_id=row["region_id"],
        t_onset=row["t_onset"],
        t_peak=row["t_peak"],
        t_end=row["t_end"],
        driver=Driver(row["driver"]),
        peak_value=row["peak_value"],
        trigger_value=row["trigger_value"],
        threshold_value=row["threshold_value"],
        rationale=row["rationale"],
        classifier=row["classifier"],
        confidence=row["confidence"],
    )


async def detect_and_persist(
    obs: FusedObservation,
    window: list[FusedObservation] | None = None,
    *,
    classifier: MLClassifierStub | TrainedClassifier | None = None,
) -> dict[str, list[Event]]:
    """
    Run all rules against the latest observation, persist transitions.

    Returns:
        {
          "onset": [Event, ...],     # newly-opened events (good to broadcast)
          "ended": [Event, ...],     # events that closed this tick
          "ongoing": [Event, ...],   # peaked / still-active events
        }
    """
    onset: list[Event] = []
    ended: list[Event] = []
    ongoing: list[Event] = []

    engine = get_engine()
    async with engine.begin() as conn:
        for rule in RULES:
            existing_row = await _open_event_for_rule(conn, rule.event_type)
            existing = _row_to_event(existing_row) if existing_row else None
            result: DetectionResult = evaluate_rule(rule, obs, existing)

            if result.new_event is not None:
                ev = result.new_event
                ev.classifier = classifier.name if classifier else "rule"
                if classifier:
                    pred = classifier.classify(window or [obs])
                    if pred is not None:
                        _, ev.confidence = pred
                values = {
                    "event_type": ev.event_type.value,
                    "state": ev.state.value,
                    "severity": ev.severity,
                    "region_id": ev.region_id,
                    "t_onset": ev.t_onset,
                    "t_peak": ev.t_peak,
                    "t_end": ev.t_end,
                    "driver": ev.driver.value,
                    "peak_value": ev.peak_value,
                    "trigger_value": ev.trigger_value,
                    "threshold_value": ev.threshold_value,
                    "rationale": ev.rationale,
                    "classifier": ev.classifier,
                    "confidence": ev.confidence,
                }
                await conn.execute(insert(events_table).values(**values))
                logger.info(
                    "Event ONSET: %s severity=%s value=%.4g",
                    ev.event_type.value,
                    ev.severity,
                    ev.trigger_value,
                )
                onset.append(ev)

            elif result.update_existing is not None and existing_row is not None:
                upd = result.update_existing
                stmt = update(events_table).where(events_table.c.id == existing_row["id"]).values(**upd)
                await conn.execute(stmt)
                if upd.get("state") == EventState.ENDED.value:
                    existing.state = EventState.ENDED
                    existing.t_end = upd.get("t_end")
                    ended.append(existing)
                    logger.info(
                        "Event ENDED: %s severity=%s",
                        existing.event_type.value,
                        existing.severity,
                    )
                else:
                    existing.state = EventState.PEAK
                    if "peak_value" in upd:
                        existing.peak_value = upd["peak_value"]
                        existing.severity = upd.get("severity", existing.severity)
                    ongoing.append(existing)

    # A7: persist a training sample for every detection tick. Uses the rule
    # decision as ground truth and both the champion + challenger predictions
    # as model outputs. Operator feedback (if any) is attached later.
    try:
        _features = featurize(obs, window or [obs])
        _rule_label = (
            (onset[0].event_type.value if onset else None)
            or (ongoing[0].event_type.value if ongoing else None)
            or EventType.BACKGROUND.value
        )
        _ml_label: str | None = None
        _ml_conf: float | None = None
        if classifier is not None:
            pred = classifier.classify(window or [obs])
            if pred is not None:
                _ml_label = pred[0].value
                _ml_conf = float(pred[1])

        # Shadow challenger prediction (if a challenger has been registered)
        _challenger_label: str | None = None
        _challenger_conf: float | None = None
        try:
            challenger_row = await feedback_store.challenger_model_version()
            if challenger_row is not None:
                cclf = get_challenger(challenger_row["artifact_path"])
                if cclf is not None:
                    cpred = cclf.classify(window or [obs])
                    if cpred is not None:
                        _challenger_label = cpred[0].value
                        _challenger_conf = float(cpred[1])
        except Exception as exc:
            logger.debug("shadow challenger inference skipped: %s", exc)

        await feedback_store.record_sample(
            features=_features,
            rule_label=_rule_label,
            ml_label=_ml_label,
            ml_confidence=_ml_conf,
            region_id=obs.region.region_id,
            challenger_label=_challenger_label,
            challenger_confidence=_challenger_conf,
        )
    except Exception as exc:
        logger.debug("training-sample persistence failed: %s", exc)

    return {"onset": onset, "ended": ended, "ongoing": ongoing}


async def list_events(limit: int = 50, only_open: bool = False) -> list[dict]:
    """Read recent events, newest first. Useful for an /events endpoint."""
    engine = get_engine()
    async with engine.begin() as conn:
        stmt = select(events_table).order_by(events_table.c.t_onset.desc()).limit(limit)
        if only_open:
            stmt = stmt.where(events_table.c.state != EventState.ENDED.value)
        result = await conn.execute(stmt)
        return [dict(r) for r in result.mappings().all()]
