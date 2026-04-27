"""
B3 — Pre-computed scenario datasets.

Generates GeoJSON / KMZ / Earth Studio keyframe CSV artifacts for every
scenario in app/static/scenarios.json with a concrete time window, and
writes them under `app/static/scenarios/<id>/` so the simulation page
can serve fixed assets to browser clients without re-running the fusion +
impact pipeline on every load.

Why pre-compute:
  - Zero-DB-roundtrip page loads → snappy demo for investors / customers
  - CDN-friendly (static-served files, immutable per scenario)
  - Decoupled from the live noaa_snapshots table — historical scenarios
    keep working even if the local DB is wiped or rotated
  - Reproducible — running this against a freshly-backfilled DB twice
    produces byte-identical output

Scenarios with `start: "live-*"` are skipped (they're meant to be served
live from the API). Scenarios that reference a `source_storm` inherit
the parent storm's data window for backfill validation but emit a
narrower, focused export.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.outputs.earth_studio import (
    geojson_to_keyframes_csv,
    geojson_to_kmz,
)
from app.outputs.earth_studio_recipe import (
    recipe_to_camera_csv,
    validate_recipe,
)
from app.outputs.scenario_export import export_scenario

logger = logging.getLogger(__name__)


CATALOG_PATH = Path(__file__).parent.parent / "static" / "scenarios.json"
OUTPUT_ROOT = Path(__file__).parent.parent / "static" / "scenarios"
MANIFEST_FILENAME = "manifest.json"


def _short_hash(blob: bytes) -> str:
    """8-char content hash used for cache-busting query strings."""
    return hashlib.sha256(blob).hexdigest()[:8]


def load_manifest(scenario_id: str) -> dict | None:
    """Return the per-scenario manifest if precompute has run, else None."""
    p = OUTPUT_ROOT / scenario_id / MANIFEST_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _load_catalog() -> list[dict]:
    return json.loads(CATALOG_PATH.read_text()).get("scenarios", [])


def _is_concrete_window(sc: dict) -> bool:
    """A scenario is precomputable when start + end are real ISO timestamps."""
    s = str(sc.get("start", ""))
    return (s.startswith("20") or s.startswith("19")) and not s.startswith("live")


async def precompute_scenario(sc: dict) -> dict[str, Any]:
    """
    Generate the three artifact files for one scenario.

    Returns a result dict suitable for the API response and CLI logging:
      {scenario_id, written: [paths], n_features, n_snapshots, skipped_reason}
    """
    sid = sc["id"]
    if not _is_concrete_window(sc):
        return {"scenario_id": sid, "written": [], "skipped_reason": "live_window"}

    try:
        t_start = datetime.fromisoformat(sc["start"].replace("Z", "+00:00"))
        t_end = datetime.fromisoformat(sc["end"].replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        return {"scenario_id": sid, "written": [], "skipped_reason": f"bad_window:{exc}"}

    region_filter = sc.get("region_filter")
    if isinstance(region_filter, str):
        region_filter = [r.strip() for r in region_filter.split(",") if r.strip()]

    step = int(sc.get("step_seconds", 0) or 0)
    geometry = sc.get("geometry", "polygon")  # "point" yields ~5x smaller assets
    fc, meta = await export_scenario(
        start=t_start,
        end=t_end,
        fmt="geojson",
        step_seconds=step,
        region_filter=region_filter,
        max_snapshots=2000,
        geometry=geometry,
    )

    n_features = len(fc.get("features", []))
    if n_features == 0:
        return {
            "scenario_id": sid,
            "written": [],
            "skipped_reason": "no_features",
            "n_snapshots": meta.get("downsampled_count", 0),
        }

    out_dir = OUTPUT_ROOT / sid
    out_dir.mkdir(parents=True, exist_ok=True)

    geojson_path = out_dir / "scenario.geojson"
    kmz_path = out_dir / "scenario.kmz"
    keyframes_path = out_dir / "keyframes.csv"

    geojson_bytes = json.dumps(fc, separators=(",", ":")).encode("utf-8")
    kmz_bytes_blob = geojson_to_kmz(
        fc,
        document_name=f'IonShield · {sc.get("title", sid)}',
    )
    keyframes_bytes_str = geojson_to_keyframes_csv(fc).encode("utf-8")

    geojson_path.write_bytes(geojson_bytes)
    kmz_path.write_bytes(kmz_bytes_blob)
    keyframes_path.write_bytes(keyframes_bytes_str)

    # B4 caveat 2 fix: also emit Earth Studio camera-track CSV. This is the
    # native column layout Earth Studio's Tracks tool reads, so the operator
    # gets a fully-keyframed camera animation by drag-dropping the file
    # rather than hand-keying the recipe waypoints.
    camera_csv_bytes: bytes | None = None
    recipe_issues: list[str] = []
    recipe = sc.get("recipe")
    if recipe:
        recipe_issues = validate_recipe(recipe)
        if not recipe_issues:
            try:
                camera_csv_bytes = recipe_to_camera_csv(recipe).encode("utf-8")
                (out_dir / "camera.csv").write_bytes(camera_csv_bytes)
            except Exception as exc:
                logger.warning("camera.csv emit failed for %s: %s", sid, exc)

    # Per-file content hashes for cache-busting query strings (caveat 3 fix).
    # When the precompute output changes, the catalog returns new ?v= values
    # so browsers cached on the old asset re-fetch automatically.
    manifest = {
        "scenario_id": sid,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "n_features": n_features,
        "n_snapshots": meta.get("downsampled_count", 0),
        "files": {
            "scenario.geojson": {
                "hash": _short_hash(geojson_bytes),
                "bytes": len(geojson_bytes),
            },
            "scenario.kmz": {
                "hash": _short_hash(kmz_bytes_blob),
                "bytes": len(kmz_bytes_blob),
            },
            "keyframes.csv": {
                "hash": _short_hash(keyframes_bytes_str),
                "bytes": len(keyframes_bytes_str),
            },
            **(
                {
                    "camera.csv": {
                        "hash": _short_hash(camera_csv_bytes),
                        "bytes": len(camera_csv_bytes),
                    }
                }
                if camera_csv_bytes
                else {}
            ),
        },
        "recipe_issues": recipe_issues,
    }
    (out_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))

    return {
        "scenario_id": sid,
        "written": [
            f"/static/scenarios/{sid}/scenario.geojson",
            f"/static/scenarios/{sid}/scenario.kmz",
            f"/static/scenarios/{sid}/keyframes.csv",
        ],
        "n_features": n_features,
        "n_snapshots": meta.get("downsampled_count", 0),
        "geojson_bytes": len(geojson_bytes),
        "kmz_bytes": len(kmz_bytes_blob),
        "keyframes_bytes": len(keyframes_bytes_str),
        "manifest": manifest,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


async def precompute_all(only_id: str | None = None) -> list[dict]:
    """Precompute every concrete-window scenario. Idempotent (overwrites)."""
    catalog = _load_catalog()
    results: list[dict] = []
    for sc in catalog:
        if only_id and sc.get("id") != only_id:
            continue
        try:
            results.append(await precompute_scenario(sc))
        except Exception as exc:
            logger.warning("Precompute %s failed: %s", sc.get("id"), exc)
            results.append({"scenario_id": sc.get("id"), "written": [], "skipped_reason": f"error:{exc}"})
    return results
