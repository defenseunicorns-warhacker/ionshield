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

    # Comma-separated CORS origins. Use "*" to allow all (open data use case).
    cors_origins: str = "*"

    # NOAA fetch interval and HTTP timeout (seconds)
    refresh_interval_seconds: int = 300
    noaa_timeout_seconds: float = 10.0

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
    smtp_host:       str       = ""
    smtp_port:       int       = 587
    smtp_username:   str       = ""
    smtp_password:   SecretStr = SecretStr("")
    smtp_from_email: str       = "noreply@ionshield.io"
    smtp_tls:        bool      = True   # STARTTLS on port 587

    # Where pilot inquiry notifications are delivered
    contact_to_email: str = "pilots@ionshield.io"

    # Rate limit for the contact form (separate from API rate limit).
    # Default is 100/hour — enough for E2E tests and real users.
    # Set CONTACT_RATE_LIMIT=5/hour in production to tighten against spam.
    contact_rate_limit: str = "100/hour"

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
