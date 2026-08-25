"""API settings."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    database_url: str = Field(default="postgresql://fx:fx@localhost:5432/fx", alias="DATABASE_URL")

    ws_ticket_ttl_s: int = Field(default=30, alias="FX_WS_TICKET_TTL_S")
    # ON by default. The demo posture used to be the default posture, which meant
    # the stream was open to anyone who could reach the port unless someone
    # remembered to flip a flag. Local development opts out explicitly with
    # FX_REQUIRE_WS_TICKET=0; the compose file does exactly that.
    require_ws_ticket: bool = Field(default=True, alias="FX_REQUIRE_WS_TICKET")
    ws_slow_client_deadline_s: float = Field(default=10.0, alias="FX_WS_SLOW_CLIENT_DEADLINE_S")

    # Applied to the two endpoints that do real work per request: ticket issuance
    # (which writes to Redis) and the estimator comparison (which reads up to
    # 1,440 bars and runs five estimators over them).
    rate_limit_per_min: int = Field(default=30, alias="FX_RATE_LIMIT_PER_MIN")
    ticket_limit_per_min: int = Field(default=20, alias="FX_TICKET_LIMIT_PER_MIN")
    # Estimators only change when a bar seals, so anything under a minute is
    # correct. 30s halves the worst case while keeping the panel feeling live.
    compare_cache_s: float = Field(default=30.0, alias="FX_COMPARE_CACHE_S")

    cors_origins: str = Field(default="http://localhost:5173", alias="FX_CORS_ORIGINS")
    # Feed is considered stale if the ingestor has not said "healthy" recently.
    # /readyz fails on this, so a replica with a dead feed stops taking traffic.
    feed_stale_after_s: float = Field(default=60.0, alias="FX_FEED_STALE_AFTER_S")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
