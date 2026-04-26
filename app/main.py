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
from app.config import settings
from app.data.archiver import archive_snapshot
from app.data.db import init_db
from app.data.locations import assess_all, get_active_alerts, load_locations
from app.data.noaa import fetch_noaa, get_kp

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


async def _refresh_loop() -> None:
    """Fetch NOAA data on a fixed interval, archive, then reload locations. Never fatal."""
    while True:
        await asyncio.sleep(settings.refresh_interval_seconds)
        try:
            await fetch_noaa(timeout=settings.noaa_timeout_seconds)
        except Exception as exc:
            logger.error("Unexpected error in refresh loop: %s", exc, exc_info=True)

        await archive_snapshot()
        _reload_locations()

        if settings.cot_push_enabled:
            try:
                await _push_cot()
            except Exception as exc:
                logger.warning("CoT push error: %s", exc)


# ── Lifespan ─────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    logger.info(
        "IonShield v%s starting — performing initial NOAA fetch…", settings.app_version
    )
    await init_db()
    await fetch_noaa(timeout=settings.noaa_timeout_seconds)
    await archive_snapshot()
    _reload_locations()
    from app.data.locations import location_count

    logger.info(
        "Initial fetch complete — %d location(s) loaded. Launching background refresh loop.",
        location_count(),
    )
    task = asyncio.create_task(_refresh_loop())
    yield
    task.cancel()
    try:
        await task
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
        response.headers["Permissions-Policy"] = (
            "geolocation=(), camera=(), microphone=()"
        )
        # HSTS: only set when behind TLS (PaaS platforms handle TLS termination)
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
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

    @app.get("/pricing", include_in_schema=False)
    async def mkt_pricing():
        return _page("pricing.html")

    @app.get("/compliance", include_in_schema=False)
    async def mkt_compliance():
        return _page("compliance.html")

    # ── 3D Dashboard ──────────────────────────────────────────────────────────
    @app.get("/dashboard", include_in_schema=False)
    async def dashboard():
        return FileResponse(_STATIC_DIR / "index.html")

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
        loc_tags = "\n".join(
            f"  <url><loc>{base}{u}</loc><changefreq>weekly</changefreq></url>"
            for u in urls
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + loc_tags
            + "\n</urlset>"
        )
        return Response(xml, media_type="application/xml")

    # Routes
    app.include_router(router)
    app.include_router(router_v2)

    return app


app = create_app()
