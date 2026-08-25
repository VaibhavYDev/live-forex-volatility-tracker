"""Ingestor settings. Every knob in ``.env.example`` maps to a field here."""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    provider: str = Field(default="replay", alias="FX_PROVIDER")
    provider_token: str = Field(default="", alias="FX_PROVIDER_TOKEN")
    symbols: str = Field(default="EURUSD,GBPUSD,USDJPY,AUDUSD,USDCHF", alias="FX_SYMBOLS")

    replay_ticks_per_sec: float = Field(default=25.0, alias="FX_REPLAY_TICKS_PER_SEC")
    replay_seed: int = Field(default=42, alias="FX_REPLAY_SEED")
    replay_burst_every_s: float = Field(default=90.0, alias="FX_REPLAY_BURST_EVERY_S")

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

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        allowed = {"replay", "tiingo", "twelvedata"}
        if v not in allowed:
            raise ValueError(f"FX_PROVIDER must be one of {sorted(allowed)}, got {v!r}")
        return v

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]
