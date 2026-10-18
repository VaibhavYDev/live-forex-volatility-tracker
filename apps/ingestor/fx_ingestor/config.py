"""Ingestor settings. Every knob in ``.env.example`` maps to a field here."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fx_ingestor.providers import PROVIDERS


class IngestorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    provider: str = Field(default="tiingo", alias="FX_PROVIDER")
    provider_token: str = Field(default="", alias="FX_PROVIDER_TOKEN", validate_default=True)
    symbols: str = Field(default="EURUSD,GBPUSD,USDJPY,AUDUSD,USDCHF", alias="FX_SYMBOLS")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    stream_retention_s: int = Field(default=900, alias="FX_STREAM_RETENTION_S")

    lease_ttl_ms: int = Field(default=10_000, alias="FX_LEASE_TTL_MS")
    lease_renew_ms: int = Field(default=3_000, alias="FX_LEASE_RENEW_MS")
    staleness_timeout_s: float = Field(default=30.0, alias="FX_STALENESS_TIMEOUT_S")
    backoff_base_s: float = Field(default=0.5, alias="FX_BACKOFF_BASE_S")
    backoff_cap_s: float = Field(default=60.0, alias="FX_BACKOFF_CAP_S")
    breaker_fail_threshold: int = Field(default=5, alias="FX_BREAKER_FAIL_THRESHOLD")
    breaker_reset_s: float = Field(default=30.0, alias="FX_BREAKER_RESET_S")

    bucket_seconds: int = Field(default=60, alias="FX_BUCKET_SECONDS")
    ewma_lambda: float = Field(default=0.97, alias="FX_EWMA_LAMBDA")
    metrics_port: int = Field(default=9100, alias="FX_INGESTOR_METRICS_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Minutes the feed missed, filled from the provider's own one-minute history
    # (fx_ingestor.recovery). Off by default: "dry_run" reports what it would
    # fetch and write without writing; "apply" needs migration 004.
    recovery_mode: Literal["off", "dry_run", "apply"] = Field(
        default="off", alias="FX_RECOVERY_MODE"
    )
    database_url: str = Field(default="", alias="DATABASE_URL")
    recovery_interval_s: float = Field(default=900.0, gt=0, alias="FX_RECOVERY_INTERVAL_S")
    recovery_first_delay_s: float = Field(default=60.0, ge=0, alias="FX_RECOVERY_FIRST_DELAY_S")
    # Bounded well inside the 90-day retention, so nothing is written into a
    # range retention is about to drop.
    recovery_lookback_h: float = Field(default=168.0, gt=0, le=720, alias="FX_RECOVERY_LOOKBACK_H")
    recovery_max_requests: int = Field(default=20, ge=1, le=200, alias="FX_RECOVERY_MAX_REQUESTS")
    recovery_request_spacing_s: float = Field(
        default=2.0, ge=0.5, alias="FX_RECOVERY_REQUEST_SPACING_S"
    )
    recovery_days_per_request: int = Field(
        default=3, ge=1, le=7, alias="FX_RECOVERY_DAYS_PER_REQUEST"
    )

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        if v not in PROVIDERS:
            raise ValueError(f"FX_PROVIDER must be one of {sorted(PROVIDERS)}, got {v!r}")
        return v

    @field_validator("provider_token")
    @classmethod
    def _token_present(cls, v: str) -> str:
        # No credentials means no market data, and nothing else to show instead.
        if not v.strip():
            raise ValueError("FX_PROVIDER_TOKEN is required: only real market data is served")
        return v

    @model_validator(mode="after")
    def _recovery_has_a_database(self) -> IngestorSettings:
        if self.recovery_mode != "off" and not self.database_url.strip():
            raise ValueError(
                "FX_RECOVERY_MODE needs DATABASE_URL: recovery reads and writes bars_1m"
            )
        return self

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]
