"""
IonShield FastAPI application factory.

Startup sequence:
  1. Configure structured logging
  2. Perform initial NOAA data fetch (blocking — ensures data is ready before first request)
  3. Launch background refresh loop

Security middleware applied:
  - Security headers (X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
    Permissions-Policy, HSTS — TLS termination handled by proxy/PaaS)
  - CORS (configurable via CORS_ORIGINS env var)
  - Rate limiting error handler (429 responses)

Deprecation fix: uses lifespan context manager instead of deprecated
@app.on_event("startup") / @app.on_event("shutdown").
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.routes import limiter, router
from app.api.routes_v2 import router_v2
from app.api.routes_v3 import router_v3
from app.config import settings
from app.data.archiver import archive_snapshot
from app.data.db import init_db
from app.data.event_store import detect_and_persist
from app.models.ml_classifier import get_classifier as get_ml_classifier
from app.data import breaker_store
from app.data.foundry_sync import build_snapshot_payload, sync_rows, sync_snapshot
from app.data.fusion import fuse_snapshot
from app.data.instrumentation import begin_loop_tick, time_stage
from app.data.locations import assess_all, get_active_alerts, load_locations
from app.data.noaa import cache_snapshot as noaa_cache_snapshot
from app.data.noaa import fetch_noaa, get_bz, get_kp, get_proton_flux_10mev
from app.data.noaa import get_wind_speed, get_xray_flux
from app.data.registry import DataSource, list_sources, register, run_all
from app.data.circuit_breaker import BreakerConfig, set_persistor
from app.data.ustec import cache_snapshot as iono_cache_snapshot
from app.data.ustec import fetch_ionosphere, get_glotec_featurecollection
from app.models.impact import assess_grid as assess_impact_grid

_STATIC_DIR = Path(__file__).parent / "static"
_PAGES_DIR = Path(__file__).parent / "pages"


# ── Logging ──────────────────────────────────────────────────────────────────


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )
    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ── Location + CoT helpers ───────────────────────────────────────────────────


def _reload_locations() -> None:
    """Reload locations.json and run risk model against current NOAA data."""
    load_locations(settings.locations_file, settings.alert_threshold)
    assess_all(get_kp())


async def _push_cot() -> None:
    """Push CoT events for in-alert locations to the configured TAK server."""
    from app.outputs.cot import push_cot_to_server

    alerts = get_active_alerts()
    if alerts:
        await push_cot_to_server(
            settings.cot_server_host,
            settings.cot_server_port,
            alerts,
            stale_minutes=settings.cot_stale_minutes,
        )


# ── Background refresh loop ──────────────────────────────────────────────────


async def _push_foundry() -> None:
    """Push the current observation snapshot to Foundry. No-op if disabled."""
    if not settings.foundry_sync_configured:
        return
    noaa_snap = noaa_cache_snapshot()
    noaa_snap.update(
        kp_index=get_kp(),
        bz_nt=get_bz(),
        xray_flux=get_xray_flux(),
        proton_flux_10mev=get_proton_flux_10mev(),
        wind_speed_km_s=get_wind_speed(),
    )
    payload = build_snapshot_payload(noaa_snap, iono_cache_snapshot())
    await sync_snapshot(
        payload,
        stack_url=settings.foundry_stack_url,
        dataset_rid=settings.foundry_space_weather_raw_rid,
        token=settings.foundry_token.get_secret_value(),
    )


async def _detect_events() -> None:
    """Run the rule-based event detector on the latest fused observation.

    Uses the trained ML classifier when its artifact is bundled (preferred),
    falling back to the rule-derived MLClassifierStub when not.
    """
    iono_snap = iono_cache_snapshot()
    fused_one = fuse_snapshot(
        when=None,
        kp=get_kp(),
        bz_nt=get_bz(),
        wind_speed_km_s=get_wind_speed(),
        xray_flux_wm2=get_xray_flux(),
        proton_flux_10mev_pfu=get_proton_flux_10mev(),
        f107_sfu=iono_snap.get("f107_sfu", 70.0),
        glotec_fc=None,  # detector is region-agnostic for global rules
    )
    if not fused_one:
        return
    classifier = get_ml_classifier()
    try:
        out = await detect_and_persist(fused_one[0], classifier=classifier)
    except Exception as exc:
        logger.warning("Event detection error: %s", exc)
        return

    # Optional Foundry events sync — only push transitions worth recording.
    if not settings.foundry_events_rid or not settings.foundry_sync_configured:
        return
    rows = [e.to_dict() for e in (out["onset"] + out["ended"])]
    if not rows:
        return
    await sync_rows(
        rows,
        stack_url=settings.foundry_stack_url,
        dataset_rid=settings.foundry_events_rid,
        token=settings.foundry_token.get_secret_value(),
    )


async def _push_foundry_impact() -> None:
    """Push per-region impact rows (A4) to Foundry. No-op if disabled."""
    if not (settings.foundry_sync_configured and settings.foundry_impact_rid):
        return
    iono_snap = iono_cache_snapshot()
    fused = fuse_snapshot(
        when=None,
        kp=get_kp(),
        bz_nt=get_bz(),
        wind_speed_km_s=get_wind_speed(),
        xray_flux_wm2=get_xray_flux(),
        proton_flux_10mev_pfu=get_proton_flux_10mev(),
        f107_sfu=iono_snap.get("f107_sfu", 70.0),
        glotec_fc=get_glotec_featurecollection(),
    )
    impacts = assess_impact_grid(fused)
    rows = [r for ia in impacts for r in ia.to_rows()]
    await sync_rows(
        rows,
        stack_url=settings.foundry_stack_url,
        dataset_rid=settings.foundry_impact_rid,
        token=settings.foundry_token.get_secret_value(),
    )


async def _push_foundry_fused() -> None:
    """Push the fused Region × Time grid to Foundry. No-op if disabled."""
    if not settings.foundry_fused_sync_configured:
        return
    iono_snap = iono_cache_snapshot()
    fused = fuse_snapshot(
        when=None,
        kp=get_kp(),
        bz_nt=get_bz(),
        wind_speed_km_s=get_wind_speed(),
        xray_flux_wm2=get_xray_flux(),
        proton_flux_10mev_pfu=get_proton_flux_10mev(),
        f107_sfu=iono_snap.get("f107_sfu", 70.0),
        glotec_fc=get_glotec_featurecollection(),
        feed_quality={**(noaa_cache_snapshot().get("fetch_status") or {}), **(iono_snap.get("fetch_status") or {})},
        data_age_seconds=int(noaa_cache_snapshot().get("data_age_seconds") or 0),
    )
    rows = [obs.to_dict() for obs in fused]
    await sync_rows(
        rows,
        stack_url=settings.foundry_stack_url,
        dataset_rid=settings.foundry_location_risk_rid,
        token=settings.foundry_token.get_secret_value(),
    )


def _register_default_sources() -> None:
    """Register the built-in NOAA + ionosphere sources with the registry.

    In OFFLINE_MODE no sources are registered: run_all() becomes a no-op,
    and the platform serves archived snapshots + precomputed scenarios.
    Decision confidence reports staleness honestly.
    """
    if settings.offline_mode:
        logger.warning("OFFLINE_MODE enabled — external data fetches disabled. " "Serving archived/replay data only.")
        return

    from app.data.noaa import cache_snapshot as _noaa_status
    from app.data.ustec import cache_snapshot as _iono_status
    from app.data import donki as _donki
    from app.data import drap as _drap
    from app.data import nanu as _nanu
    from app.data import ovation as _ovation

    register(
        DataSource(
            name="noaa_swpc",
            cadence_seconds=settings.refresh_interval_seconds,
            fetch_async=lambda timeout: fetch_noaa(timeout=timeout),
            status_async=_noaa_status,
            timeout_seconds=settings.noaa_timeout_seconds,
            breaker_config=BreakerConfig(failure_threshold=4, cooldown_seconds=300),
        )
    )
    register(
        DataSource(
            name="ionosphere",
            cadence_seconds=settings.refresh_interval_seconds,
            fetch_async=lambda timeout: fetch_ionosphere(timeout=timeout),
            status_async=_iono_status,
            timeout_seconds=settings.noaa_timeout_seconds,
            breaker_config=BreakerConfig(failure_threshold=4, cooldown_seconds=300),
        )
    )
    # Authoritative HF absorption (replaces the X-ray-flux proxy where live).
    register(
        DataSource(
            name="drap",
            cadence_seconds=settings.refresh_interval_seconds,
            fetch_async=lambda timeout: _drap.fetch_drap(timeout=timeout),
            status_async=_drap.cache_snapshot,
            timeout_seconds=settings.noaa_timeout_seconds,
            breaker_config=BreakerConfig(failure_threshold=4, cooldown_seconds=300),
        )
    )
    # GPS availability: live constellation status from CelesTrak GPS-ops, or a
    # NANU mirror when NANU_URL is set.
    register(
        DataSource(
            name="nanu",
            cadence_seconds=settings.refresh_interval_seconds,
            fetch_async=lambda timeout: _nanu.fetch_nanu(timeout=timeout),
            status_async=_nanu.cache_snapshot,
            timeout_seconds=settings.noaa_timeout_seconds,
            breaker_config=BreakerConfig(failure_threshold=4, cooldown_seconds=300),
        )
    )
    # NASA DONKI — space-weather event log (cause-of-risk / timeline).
    register(
        DataSource(
            name="donki",
            cadence_seconds=settings.refresh_interval_seconds,
            fetch_async=lambda timeout: _donki.fetch_donki(timeout=timeout),
            status_async=_donki.cache_snapshot,
            timeout_seconds=settings.noaa_timeout_seconds,
            breaker_config=BreakerConfig(failure_threshold=4, cooldown_seconds=300),
        )
    )
    # OVATION aurora — high-latitude GNSS/comms scintillation indicator.
    register(
        DataSource(
            name="ovation",
            cadence_seconds=settings.refresh_interval_seconds,
            fetch_async=lambda timeout: _ovation.fetch_ovation(timeout=timeout),
            status_async=_ovation.cache_snapshot,
            timeout_seconds=settings.noaa_timeout_seconds,
            breaker_config=BreakerConfig(failure_threshold=4, cooldown_seconds=300),
        )
    )


_egress_locks: dict[str, asyncio.Semaphore] = {}


async def _fire_and_forget(coro_factory, *, label: str) -> None:
    """
    Run a coroutine in the background; log but don't propagate errors.

    Bounded by a per-label Semaphore(1): if the previous task with the same
    label is still running (e.g. a hung Foundry transaction), this tick's
    push is **skipped** with a warning rather than queued. This prevents
    unbounded task accumulation when an egress target stalls indefinitely.
    """
    sem = _egress_locks.setdefault(label, asyncio.Semaphore(1))
    if sem.locked():
        logger.warning(
            "Background task %s skipped — previous run still in flight",
            label,
        )
        return

    async def _wrapper() -> None:
        async with sem:
            try:
                await coro_factory()
            except Exception as exc:
                logger.warning("Background task %s failed: %s", label, exc)

    asyncio.create_task(_wrapper())


async def _refresh_loop() -> None:
    """
    Single tick: parallel source fetches via registry → instrumented stages →
    fire-and-forget egress → reload locations. Never fatal.
    """
    while True:
        await asyncio.sleep(settings.refresh_interval_seconds)
        begin_loop_tick()

        with time_stage("fetch"):
            try:
                results = await run_all()
            except Exception as exc:
                logger.error("Refresh loop error: %s", exc, exc_info=True)
                results = {}
        if results:
            logger.debug("Source results: %s", results)
        _persist_feed_state(results)

        with time_stage("archive"):
            await archive_snapshot()

        # Egress: don't block the loop on slow Foundry transactions.
        await _fire_and_forget(_push_foundry, label="foundry_raw")
        await _fire_and_forget(_push_foundry_fused, label="foundry_fused")
        await _fire_and_forget(_push_foundry_impact, label="foundry_impact")

        with time_stage("detect"):
            await _detect_events()

        with time_stage("reload"):
            _reload_locations()

        if settings.cot_push_enabled:
            await _fire_and_forget(_push_cot, label="cot_push")


def _persist_feed_state(results: dict) -> None:
    """Cache-and-carry: persist feed state after a successful live cycle.

    Any source reporting success counts — partial state beats no state in a
    later air-gapped boot. A live save also supersedes any carried state.
    """
    from app.data import state_cache

    if settings.offline_mode or not results:
        return
    # run_all() returns source.name → "ok" | "skipped" | "timeout" | "error"
    if any(status == "ok" for status in results.values()):
        if state_cache.save_state():
            state_cache.mark_live()


# ── Lifespan ─────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    logger.info("IonShield v%s starting — performing initial NOAA fetch…", settings.app_version)
    await init_db()
    set_persistor(breaker_store.persist)
    _register_default_sources()
    # Rehydrate breakers from DB so an OPEN state recorded before the last
    # restart survives the restart (single-replica). For scale-out, point
    # `set_persistor` and `breaker_store.hydrate_all` at a shared backend.
    persisted = await breaker_store.hydrate_all()
    for src in list_sources():
        src.breaker.hydrate(persisted.get(src.name))

    # Cache-and-carry: rehydrate the last persisted feed state before the
    # first fetch. In OFFLINE_MODE this is the data source (ADVISORY mode);
    # online it bridges the gap until the first live fetch lands (which
    # overwrites it and supersedes the carried state).
    from app.data import state_cache

    state_cache.hydrate()

    results = await run_all()
    _persist_feed_state(results)
    await archive_snapshot()
    await _fire_and_forget(_push_foundry, label="foundry_raw_initial")
    await _fire_and_forget(_push_foundry_fused, label="foundry_fused_initial")
    await _fire_and_forget(_push_foundry_impact, label="foundry_impact_initial")
    await _detect_events()
    _reload_locations()
    from app.data.locations import location_count

    logger.info(
        "Initial fetch complete — %d location(s) loaded. Launching background refresh loop.",
        location_count(),
    )
    task = asyncio.create_task(_refresh_loop())

    # B3 caveat fix: auto-run scenario precompute in the background after the
    # first refresh tick, so a fresh deploy populates app/static/scenarios/
    # without needing the operator to invoke scripts/precompute_scenarios.sh.
    # Idempotent — does nothing if assets are already up-to-date.
    async def _bootstrap_scenarios() -> None:
        try:
            from app.data import historical_backfill, scenario_precompute

            # Backfill hits NASA CDAWeb HAPI — skip when air-gapped. Scenario
            # precompute still runs against whatever the local DB holds.
            if not settings.offline_mode:
                await historical_backfill.backfill_all_predefined()
            results = await scenario_precompute.precompute_all()
            written = sum(1 for r in results if r.get("written"))
            logger.info(
                "Scenario precompute bootstrap: %d/%d scenarios written",
                written,
                len(results),
            )
        except Exception as exc:
            logger.warning("Scenario bootstrap failed: %s", exc)

    asyncio.create_task(_bootstrap_scenarios())

    # Phase 2: train the Kp forecaster on first deploy if its artifact is
    # absent. After backfill+a-few-live-snapshots have populated the DB,
    # this gives us a useful model in the first ~5 minutes; it can be
    # retrained any time via POST /api/v3/forecast/kp/retrain.
    async def _bootstrap_kp_forecaster() -> None:
        try:
            from app.models import kp_forecaster as kpf

            if kpf.load() is None:
                # Wait one cycle so backfill has a chance to seed snapshots.
                await asyncio.sleep(60)
                artifact = await kpf.train_from_db()
                logger.info(
                    "Kp forecaster bootstrap: source=%s n_real=%d rmse=%s",
                    artifact["training_source"],
                    artifact["n_train_real"],
                    artifact["metrics"]["rmse_per_horizon"],
                )
        except Exception as exc:
            logger.warning("Kp forecaster bootstrap failed: %s", exc)

    asyncio.create_task(_bootstrap_kp_forecaster())

    # Auto-pilot loop — drift-driven retrain, challenger auto-promote, sample
    # archive. Independent of the refresh loop; safe to disable via config.
    from app.models.auto_pilot import run_loop as auto_pilot_loop

    auto_task = asyncio.create_task(auto_pilot_loop())

    yield
    task.cancel()
    auto_task.cancel()
    for t in (task, auto_task):
        try:
            await t
        except asyncio.CancelledError:
            pass
    logger.info("IonShield shutting down.")


# ── App factory ───────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Space weather operational intelligence platform. "
            "Translates real-time NOAA SWPC data into mission-relevant risk assessments "
            "for GPS, HF communications, SATCOM, and radar operations."
        ),
        lifespan=lifespan,
        # Move built-in Swagger/ReDoc to /api-docs and /api-redoc so that
        # our marketing /docs page is served without conflict.
        docs_url="/api-docs",
        redoc_url="/api-redoc",
    )

    # Rate limiter state
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key"],
        max_age=600,
    )

    # Security headers middleware
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        # HSTS: only set when behind TLS (PaaS platforms handle TLS termination)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        # Cache policy. HTML pages and the un-hashed nav.js must always
        # revalidate so a redeploy is visible immediately (no stale framing /
        # no hard-refresh required). Content-hashed build assets
        # (/static/assets/*, /static/cesium/*) keep their long immutable cache.
        ct = response.headers.get("content-type", "")
        path = request.url.path
        if ct.startswith("text/html") or path.endswith("/nav.js"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif path.startswith("/static/assets/") or path.startswith("/static/cesium/"):
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        return response

    # Phase 1: audit log — record every /api/v3/* request with resolved tenant.
    @app.middleware("http")
    async def audit_log_mw(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if not path.startswith("/api/v3/"):
            return response
        # Don't audit metrics scrapes or open admin probe endpoints.
        if path in ("/api/v3/metrics", "/api/v3/health"):
            return response
        try:
            from app.data import audit_log

            await audit_log.record(
                tenant_id=getattr(request.state, "tenant_id", None),
                key_id=getattr(request.state, "key_id", None),
                method=request.method,
                path=path,
                status_code=response.status_code,
                remote_addr=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
        except Exception:  # never block the response on audit failure
            pass
        return response

    # Static assets (CSS, JS)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    # ── Marketing pages ───────────────────────────────────────────────────────
    def _page(name: str):
        """Helper: return a FileResponse for a marketing page."""
        return FileResponse(_PAGES_DIR / name, media_type="text/html")

    @app.get("/", include_in_schema=False)
    async def marketing():
        return _page("index.html")

    @app.get("/features", include_in_schema=False)
    async def mkt_features():
        return _page("features.html")

    @app.get("/demo", include_in_schema=False)
    async def mkt_demo():
        return _page("demo.html")

    @app.get("/use-cases", include_in_schema=False)
    async def mkt_use_cases():
        return _page("use-cases.html")

    @app.get("/docs", include_in_schema=False)
    async def mkt_docs():
        return _page("docs.html")

    @app.get("/ml", include_in_schema=False)
    async def mkt_ml():
        return _page("ml.html")

    @app.get("/pricing", include_in_schema=False)
    async def mkt_pricing():
        return _page("pricing.html")

    @app.get("/compliance", include_in_schema=False)
    async def mkt_compliance():
        return _page("compliance.html")

    @app.get("/mission", include_in_schema=False)
    async def mkt_mission():
        return _page("mission.html")

    @app.get("/atak", include_in_schema=False)
    async def mkt_atak():
        return _page("atak.html")

    @app.get("/foundry", include_in_schema=False)
    async def mkt_foundry():
        return _page("foundry.html")

    @app.get("/integrations", include_in_schema=False)
    async def mkt_integrations():
        return _page("integrations.html")

    @app.get("/api-console", include_in_schema=False)
    async def mkt_api_console():
        return _page("api_console.html")

    # ── 3D Dashboard ──────────────────────────────────────────────────────────
    @app.get("/dashboard", include_in_schema=False)
    async def dashboard():
        return FileResponse(_STATIC_DIR / "index.html")

    # ── B5: Simulation / Storm Replay Mode ────────────────────────────────────
    @app.get("/simulation", include_in_schema=False)
    async def simulation():
        return _page("simulation.html")

    # ── SEO helpers ───────────────────────────────────────────────────────────
    @app.get("/robots.txt", include_in_schema=False)
    async def robots():
        content = (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /api/\n"
            "Disallow: /static/\n"
            "Sitemap: https://ionshield.io/sitemap.xml\n"
        )
        return Response(content, media_type="text/plain")

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap():
        base = "https://ionshield.io"
        urls = [
            "/",
            "/features",
            "/demo",
            "/use-cases",
            "/docs",
            "/pricing",
            "/compliance",
        ]
        loc_tags = "\n".join(f"  <url><loc>{base}{u}</loc><changefreq>weekly</changefreq></url>" for u in urls)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + loc_tags + "\n</urlset>"
        )
        return Response(xml, media_type="application/xml")

    # Routes
    app.include_router(router)
    app.include_router(router_v2)
    app.include_router(router_v3)

    return app


app = create_app()
