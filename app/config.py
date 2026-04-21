"""
IonShield configuration — all values configurable via environment variables or .env file.
"""
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
    cot_server_port: int = 8087   # standard TAK server TCP CoT port
    cot_stale_minutes: int = 10   # CoT event stale time

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
