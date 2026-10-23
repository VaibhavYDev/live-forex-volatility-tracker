"""Ingestor settings. Every knob in ``.env.example`` maps to a field here."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fx_ingestor.providers import PROVIDERS
from fx_ingestor.quota import QuotaLimits


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
    # The account's REST allowance (Tiingo Starter: 50 an hour, 1,000 a day), shared
    # by every request made with FX_PROVIDER_TOKEN, and the part recovery leaves to
    # the live feed: the hourly refresh (5), two leader changes with empty caches
    # (2 x 10) and 5 spare an hour; a day of refreshes (120), 20 leader changes
    # (200) and 80 spare a day. See fx_ingestor.quota.
    provider_requests_per_hour: int = Field(default=50, ge=1, alias="FX_PROVIDER_REQUESTS_PER_HOUR")
    provider_requests_per_day: int = Field(
        default=1_000, ge=1, alias="FX_PROVIDER_REQUESTS_PER_DAY"
    )
    recovery_reserve_per_hour: int = Field(default=30, ge=0, alias="FX_RECOVERY_RESERVE_PER_HOUR")
    recovery_reserve_per_day: int = Field(default=400, ge=0, alias="FX_RECOVERY_RESERVE_PER_DAY")

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
        if (
            self.recovery_reserve_per_hour >= self.provider_requests_per_hour
            or self.recovery_reserve_per_day >= self.provider_requests_per_day
        ):
            raise ValueError("the recovery reserve must leave part of the provider allowance")
        return self

    @property
    def quota_limits(self) -> QuotaLimits:
        return QuotaLimits(
            self.provider_requests_per_hour,
            self.provider_requests_per_day,
            self.recovery_reserve_per_hour,
            self.recovery_reserve_per_day,
        )

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]
