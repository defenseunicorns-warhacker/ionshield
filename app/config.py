"""
IonShield configuration — all values configurable via environment variables or .env file.
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    app_name: str = "IonShield API"
    app_version: str = "3.0"

    # Auth: set API_KEY to a non-empty string to require X-API-Key header.
    # Leave empty to run in open mode (suitable for local dev / internal nets).
    api_key: str = ""

    # Phase 1: bootstrap secret for the /api/v3/admin/keys mint endpoint.
    # Set this in the Render dashboard once; rotate by changing the env var.
    # Required to mint the first per-tenant Bearer key. Empty = admin disabled.
    admin_bearer: str = ""

    # Comma-separated CORS origins. Use "*" to allow all (open data use case).
    cors_origins: str = "*"

    # NOAA fetch interval and HTTP timeout (seconds)
    refresh_interval_seconds: int = 300
    noaa_timeout_seconds: float = 10.0

    # ── Air-gap / disconnected operation ────────────────────────────────────
    # OFFLINE_MODE=true disables all external data fetches (NOAA SWPC, NASA
    # HAPI backfill). The platform serves the last archived snapshot,
    # precomputed scenarios, and storm replay. Decision confidence honestly
    # reports data staleness — no fabricated freshness.
    offline_mode: bool = False
    # Cache-and-carry: every successful fetch persists the full feed state
    # here; an OFFLINE_MODE boot rehydrates from it, so a pre-mission sync
    # carries real observations + the 3-day forecast into the disconnected
    # window (ADVISORY mode). Point at the persistent volume in Kubernetes
    # (e.g. /data/last_known_state.json). Empty string disables.
    state_cache_file: str = "last_known_state.json"
    # Point at an in-enclave SWPC mirror/relay instead of the public internet
    # (e.g. a one-way diode replicator or cross-domain feed proxy).
    swpc_base_url: str = "https://services.swpc.noaa.gov"
    # NASA CDAWeb HAPI endpoint used only by historical backfill.
    hapi_base_url: str = "https://cdaweb.gsfc.nasa.gov/hapi"
    # Optional NANU (GPS outage advisory) JSON endpoint. There is no public
    # machine-readable NANU API, so this is empty by default → NANU reports
    # "unavailable" (honest). Point at an internal/enclave NANU mirror to
    # enable live ingest. (D-RAP uses SWPC_BASE_URL automatically.)
    nanu_url: str = ""
    # NASA DONKI (space-weather event notifications) API key. "DEMO_KEY" works
    # but is heavily rate-limited; set a free api.nasa.gov key for real quota.
    # DONKI goes direct to api.nasa.gov (not SWPC); in a strict air-gap it
    # falls back to cache-and-carry like every other feed.
    nasa_api_key: str = "DEMO_KEY"

    # Safety cap on route waypoints to prevent abuse
    max_route_waypoints: int = 200

    # slowapi rate limit string, e.g. "60/minute", "200/hour"
    rate_limit: str = "120/minute"

    log_level: str = "INFO"

    # ── Configurable locations ──────────────────────────────────────────────
    # Path to locations.json file (relative to working directory or absolute).
    # File is optional — location monitoring is disabled when it doesn't exist.
    locations_file: str = "locations.json"

    # Global alert threshold (per-location overrides this in locations.json).
    # Values: NOMINAL | ELEVATED | DEGRADED | SEVERE
    alert_threshold: str = "ELEVATED"

    # ── ATAK CoT push ───────────────────────────────────────────────────────
    # Set COT_SERVER_HOST to enable background CoT push to a TAK server.
    # Leave empty to disable (pull endpoint /overlay/ionshield.cot always works).
    cot_server_host: str = ""
    cot_server_port: int = 8087  # standard TAK server TCP CoT port
    cot_stale_minutes: int = 10  # CoT event stale time
    # Publish every monitored location as a colored CoT marker, not just those
    # in active alert. Gives a continuous TAK situational-awareness picture
    # (markers always present, colored by live risk). Default: alerts only.
    cot_push_all: bool = False

    # ── Observation archive / replay ─────────────────────────────────────────
    # SQLite (default) or PostgreSQL via DATABASE_URL.
    # SQLite: sqlite+aiosqlite:///./ionshield.db  (file in working directory)
    # Postgres: postgresql+asyncpg://user:pass@host/dbname
    database_url: str = "sqlite+aiosqlite:///./ionshield.db"

    # Set to false to disable observation archiving (useful in read-only deployments).
    archive_enabled: bool = True

    # ── Contact / pilot inquiry form ────────────────────────────────────────────
    # SMTP credentials — leave smtp_host empty to disable email (submissions
    # are always saved to the DB; email is best-effort on top of that).
    #
    # Provider quick-start:
    #   SendGrid SMTP:  host=smtp.sendgrid.net  port=587  user=apikey  pass=<SG.key>
    #   AWS SES SMTP:   host=email-smtp.<region>.amazonaws.com  port=587
    #   Gmail/GSuite:   host=smtp.gmail.com  port=587  (use App Password)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from_email: str = "noreply@ionshield.io"
    smtp_tls: bool = True  # STARTTLS on port 587

    # Where pilot inquiry notifications are delivered
    contact_to_email: str = "pilots@ionshield.io"

    # Rate limit for the contact form (separate from API rate limit).
    # Default is 100/hour — enough for E2E tests and real users.
    # Set CONTACT_RATE_LIMIT=5/hour in production to tighten against spam.
    contact_rate_limit: str = "100/hour"

    # ── Foundry data sync ───────────────────────────────────────────────────
    # When enabled, every refresh pushes a snapshot row to a Foundry dataset.
    # All four vars must be set; missing any disables sync silently.
    foundry_sync_enabled: bool = False
    foundry_stack_url: str = ""
    foundry_token: SecretStr = SecretStr("")
    foundry_space_weather_raw_rid: str = ""
    # Optional second dataset for fused Region × Time grid rows (A2 output).
    foundry_location_risk_rid: str = ""
    # Optional dataset for detected events (A3 output).
    foundry_events_rid: str = ""
    # Optional dataset for per-region impact rows (A4 output).
    foundry_impact_rid: str = ""
    # Optional dataset for archived training samples (A7 caveat fix).
    foundry_training_archive_rid: str = ""
    foundry_branch: str = "master"

    # Sample-archive policy: rows older than this many days are uploaded to
    # the Foundry training-archive dataset and deleted from the local DB.
    sample_archive_max_age_days: int = 30
    sample_archive_interval_seconds: int = 3600  # check every hour
    sample_archive_batch_size: int = 1000

    # B4 caveat fix: optional comma-separated allowlist of hostnames (or
    # parent suffixes) that scenario video registrations may use. Empty =
    # accept any https host. Example: "cdn.example.com,r2.example.org"
    video_domain_allowlist: str = ""

    # Auto-retrain policy: when drift agreement stays below this threshold
    # for `auto_retrain_min_samples` consecutive evaluations, trigger a
    # retrain automatically. 0 disables.
    auto_retrain_enabled: bool = True
    auto_retrain_check_interval_seconds: int = 1800  # 30 min
    auto_retrain_drift_threshold: float = 0.85
    auto_retrain_min_samples: int = 200
    auto_retrain_cooldown_seconds: int = 6 * 3600  # don't retrain more than every 6h

    # Champion/challenger A/B policy: a freshly trained model becomes the
    # challenger for this many ticks before being eligible for promotion.
    shadow_window_min_samples: int = 100
    shadow_promotion_min_advantage: float = 0.0  # challenger must equal or beat champion

    @property
    def foundry_sync_configured(self) -> bool:
        return bool(
            self.foundry_sync_enabled
            and self.foundry_stack_url
            and self.foundry_token.get_secret_value()
            and self.foundry_space_weather_raw_rid
        )

    @property
    def foundry_fused_sync_configured(self) -> bool:
        return bool(
            self.foundry_sync_enabled
            and self.foundry_stack_url
            and self.foundry_token.get_secret_value()
            and self.foundry_location_risk_rid
        )

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_username)

    @property
    def cot_push_enabled(self) -> bool:
        return bool(self.cot_server_host)

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)


settings = Settings()
