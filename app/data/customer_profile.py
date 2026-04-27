"""
B6 — Customer profile loader + scenario derivation.

A *customer profile* is a small JSON document describing how a particular
audience (defense, aerospace, commercial) wants to view the same
underlying scenarios:

  - `region_filter`     focuses the export on cells of operational interest
  - `layer_default`     picks the KML coloring driver (hf | gps | sat)
  - `branding`          accent color + watermark for the simulation page
  - `highlight_systems` decides which subsystem badges to surface in the UI

`apply_profile(scenario, profile)` returns a derived scenario dict where
the profile's overrides are merged in. Source-controlled scenarios.json is
never mutated; profiles are pure overlays.

Profiles are read from app/static/customers.json so non-engineers can edit
or add a new audience without a code change.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).parent.parent / "static" / "customers.json"


def list_profiles() -> list[dict]:
    if not CATALOG_PATH.exists():
        return []
    try:
        return json.loads(CATALOG_PATH.read_text()).get("customers", [])
    except Exception as exc:
        logger.warning("customers.json parse failed: %s", exc)
        return []


def get_profile(customer_id: str) -> dict | None:
    return next(
        (c for c in list_profiles() if c.get("id") == customer_id),
        None,
    )


def apply_profile(scenario: dict, profile: dict) -> dict:
    """
    Return a deep copy of `scenario` with `profile`'s overlay applied.

    Merge rules:
      - region_filter   → set on the derived scenario (export endpoint
                          consumes this directly)
      - layer_default   → sets `layer` (used by KML/KMZ coloring)
      - branding        → exposed to the frontend for accent + watermark
      - highlight_*     → surfaced for UI badging without altering the
                          base storm window or step
    The base scenario's id is suffixed `:<customer_id>` so derived assets
    don't collide with the base in the static-asset tree.
    """
    out = copy.deepcopy(scenario)
    cid = profile.get("id", "unknown")
    out["id"] = f"{scenario['id']}:{cid}"
    out["base_id"] = scenario["id"]
    out["customer_id"] = cid

    if profile.get("region_filter"):
        out["region_filter"] = list(profile["region_filter"])
    if profile.get("layer_default"):
        out["layer"] = profile["layer_default"]
    if profile.get("branding"):
        out["branding"] = dict(profile["branding"])
    if profile.get("highlight_systems"):
        out["highlight_systems"] = list(profile["highlight_systems"])
    if profile.get("highlight_thresholds"):
        out["highlight_thresholds"] = dict(profile["highlight_thresholds"])

    title = scenario.get("title", scenario.get("id"))
    out["title"] = f"{title} · {profile.get('title', cid)}"
    out["tagline"] = profile.get("tagline", scenario.get("tagline", ""))

    # Precomputed asset URLs become customer-scoped sub-paths.
    pc = scenario.get("precomputed")
    if pc:
        out["precomputed"] = {
            k: v.replace(
                f"/scenarios/{scenario['id']}/",
                f"/scenarios/{scenario['id']}/{cid}/",
            ) if isinstance(v, str) else v
            for k, v in pc.items()
        }

    return out


def derive_scenarios(scenarios: list[dict], customer_id: str) -> list[dict]:
    """Apply the named customer profile to every concrete scenario."""
    profile = get_profile(customer_id)
    if profile is None:
        return []
    derived: list[dict] = []
    for sc in scenarios:
        if str(sc.get("start", "")).startswith("live"):
            continue
        derived.append(apply_profile(sc, profile))
    return derived
