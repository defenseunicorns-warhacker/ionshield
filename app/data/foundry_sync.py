"""
Foundry dataset sync — pushes IonShield observation snapshots into a Foundry
dataset (e.g. space_weather_raw) so the platform's analytics layer always has
the latest values.

Foundry's Dataset API v2 is transactional:
  1. POST /api/v2/datasets/{rid}/transactions          → {rid, status:OPEN, ...}
  2. POST /api/v2/datasets/{rid}/files/{name}/upload?transactionRid={tx}
                                                       → uploads file body
  3. POST /api/v2/datasets/{rid}/transactions/{tx}/commit
                                                       → {status:COMMITTED}

Endpoints verified end-to-end against ionshield.usw-16.palantirfoundry.com on
2026-04-26 — a real test row was successfully written to space_weather_raw.

We append a single newline-delimited JSON file per snapshot. Foundry's
schema-on-read auto-detects new fields, so adding columns later doesn't
break existing rows.

Failures are non-fatal — sync errors are logged and swallowed so a Foundry
outage never takes down the API. Set FOUNDRY_SYNC_ENABLED=false to disable.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class FoundrySyncError(RuntimeError):
    """Raised on transaction failure. Always caught and logged in sync_snapshot."""


async def _start_transaction(
    client: httpx.AsyncClient,
    stack_url: str,
    dataset_rid: str,
    token: str,
    txn_type: str = "APPEND",
) -> str:
    url = f"{stack_url.rstrip('/')}/api/v2/datasets/{dataset_rid}/transactions"
    r = await client.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"transactionType": txn_type},
    )
    if r.status_code >= 400:
        raise FoundrySyncError(f"start_transaction {r.status_code}: {r.text[:200]}")
    return r.json()["rid"]


async def _put_file(
    client: httpx.AsyncClient,
    stack_url: str,
    dataset_rid: str,
    transaction_rid: str,
    token: str,
    filename: str,
    body: bytes,
) -> None:
    url = (
        f"{stack_url.rstrip('/')}/api/v2/datasets/{dataset_rid}/files/"
        f"{filename}/upload?transactionRid={transaction_rid}"
    )
    r = await client.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
        content=body,
    )
    if r.status_code >= 400:
        raise FoundrySyncError(f"put_file {r.status_code}: {r.text[:200]}")


async def _commit_transaction(
    client: httpx.AsyncClient,
    stack_url: str,
    dataset_rid: str,
    transaction_rid: str,
    token: str,
) -> None:
    url = (
        f"{stack_url.rstrip('/')}/api/v2/datasets/{dataset_rid}/"
        f"transactions/{transaction_rid}/commit"
    )
    r = await client.post(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code >= 400:
        raise FoundrySyncError(f"commit_transaction {r.status_code}: {r.text[:200]}")


async def sync_rows(
    rows: list[dict[str, Any]],
    *,
    stack_url: str,
    dataset_rid: str,
    token: str,
    timeout: float = 30.0,
) -> bool:
    """
    Append a batch of rows as a single newline-delimited JSON file.

    Used by the fused-grid sync (A2) to push a Region × Time slice (~324
    rows) in a single transaction. Behaves identically to sync_snapshot
    otherwise — never raises.
    """
    if not (stack_url and dataset_rid and token):
        logger.debug("Foundry sync_rows skipped: missing config")
        return False
    if not rows:
        logger.debug("Foundry sync_rows skipped: empty rows")
        return False

    body = ("\n".join(json.dumps(r, default=str) for r in rows) + "\n").encode("utf-8")
    filename = f"fused-{int(time.time() * 1000)}.jsonl"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            tx = await _start_transaction(client, stack_url, dataset_rid, token, "APPEND")
            await _put_file(client, stack_url, dataset_rid, tx, token, filename, body)
            await _commit_transaction(client, stack_url, dataset_rid, tx, token)
        logger.info(
            "Foundry sync_rows OK: dataset=%s file=%s rows=%d bytes=%d",
            dataset_rid, filename, len(rows), len(body),
        )
        return True
    except FoundrySyncError as exc:
        logger.warning("Foundry sync_rows failed: %s", exc)
        return False
    except Exception as exc:
        logger.warning("Foundry sync_rows error: %s", exc)
        return False


async def sync_snapshot(
    snapshot: dict[str, Any],
    *,
    stack_url: str,
    dataset_rid: str,
    token: str,
    timeout: float = 15.0,
) -> bool:
    """
    Append a single snapshot row to a Foundry dataset.

    Returns True on success, False on any failure (including auth, network, or
    transaction errors). Never raises — this function is called from the
    refresh loop and must not propagate exceptions.
    """
    if not (stack_url and dataset_rid and token):
        logger.debug("Foundry sync skipped: missing config")
        return False

    body = (json.dumps(snapshot, default=str) + "\n").encode("utf-8")
    filename = f"snapshot-{int(time.time() * 1000)}.jsonl"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            tx = await _start_transaction(
                client, stack_url, dataset_rid, token, "APPEND"
            )
            await _put_file(
                client, stack_url, dataset_rid, tx, token, filename, body
            )
            await _commit_transaction(
                client, stack_url, dataset_rid, tx, token
            )
        logger.info(
            "Foundry sync OK: dataset=%s file=%s bytes=%d",
            dataset_rid, filename, len(body),
        )
        return True
    except FoundrySyncError as exc:
        logger.warning("Foundry sync failed: %s", exc)
        return False
    except Exception as exc:
        logger.warning("Foundry sync error: %s", exc)
        return False


def build_snapshot_payload(noaa_cache: dict, iono_cache: dict) -> dict[str, Any]:
    """
    Build the JSON-shaped payload synced to Foundry. Schema is intentionally
    flat and stable so Foundry's auto-detected dataset schema remains valid
    when new fields are added (extras are appended to the right).
    """
    from datetime import datetime, timezone

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetch_source": noaa_cache.get("fetch_source"),
        "kp_index": noaa_cache.get("kp_index"),
        "bz_nt": noaa_cache.get("bz_nt"),
        "xray_flux_wm2": noaa_cache.get("xray_flux"),
        "proton_flux_10mev_pfu": noaa_cache.get("proton_flux_10mev"),
        "wind_speed_km_s": noaa_cache.get("wind_speed_km_s"),
        "kp_forecast_24h": noaa_cache.get("kp_forecast_24h"),
        "f107_sfu": iono_cache.get("f107_sfu"),
        "glotec_median_tecu": iono_cache.get("glotec_median_tecu"),
        "glotec_p95_tecu": iono_cache.get("glotec_p95_tecu"),
        "glotec_max_tecu": iono_cache.get("glotec_max_tecu"),
        "glotec_time_tag": iono_cache.get("glotec_time_tag"),
        "glotec_n_features": iono_cache.get("glotec_n_features"),
        "noaa_feed_status": noaa_cache.get("fetch_status"),
        "iono_feed_status": iono_cache.get("fetch_status"),
        "data_age_seconds": noaa_cache.get("data_age_seconds"),
    }
