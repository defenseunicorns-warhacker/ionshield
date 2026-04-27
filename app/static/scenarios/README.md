# Pre-computed scenario assets

This directory holds the static GeoJSON / KMZ / Earth Studio CSV artifacts
that the Simulation Mode page (B5) prefers over the live API export. The
assets total ~80 MB across all scenarios so they are **not committed to
git** — they're regenerated on each deploy.

## Regenerate locally

    python -m app.data.scenario_precompute    # not yet a CLI; use the API:
    curl -X POST http://localhost:8000/api/v3/scenarios/precompute

Or programmatically:

    from app.data import scenario_precompute as sp
    import asyncio
    asyncio.run(sp.precompute_all())

## Regenerate in production

After a Render deploy completes, hit:

    curl -X POST https://ionshield-demo-mvp.onrender.com/api/v3/scenarios/precompute

Idempotent — re-running overwrites. Backfill (`scripts/backfill_production.sh`)
must run first so historical-storm rows exist in `noaa_snapshots`.

## File layout

    app/static/scenarios/<scenario_id>/
        scenario.geojson        — time-indexed FeatureCollection (B1)
        scenario.kmz            — Earth Studio import (B2)
        keyframes.csv           — Earth Studio Tracks input (B2)
