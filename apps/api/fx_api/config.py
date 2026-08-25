"""API settings."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    database_url: str = Field(default="postgresql://fx:fx@localhost:5432/fx", alias="DATABASE_URL")

    ws_ticket_ttl_s: int = Field(default=30, alias="FX_WS_TICKET_TTL_S")
    # Off by default so `docker compose up` gives a reviewer a working dashboard
    # with no auth dance. The mechanism is fully implemented; this only decides
    # whether it is enforced.
    require_ws_ticket: bool = Field(default=False, alias="FX_REQUIRE_WS_TICKET")
    ws_slow_client_deadline_s: float = Field(default=10.0, alias="FX_WS_SLOW_CLIENT_DEADLINE_S")

    cors_origins: str = Field(default="http://localhost:5173", alias="FX_CORS_ORIGINS")
    # Feed is considered stale if the ingestor has not said "healthy" recently.
    # /readyz fails on this, so a replica with a dead feed stops taking traffic.
    feed_stale_after_s: float = Field(default=60.0, alias="FX_FEED_STALE_AFTER_S")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
