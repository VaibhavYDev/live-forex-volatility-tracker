"""Worker settings."""

from __future__ import annotations

import socket

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    database_url: str = Field(default="postgresql://fx:fx@localhost:5432/fx", alias="DATABASE_URL")

    # Flush on whichever comes first: bounded latency AND bounded memory.
    # Rows alone would stall on a quiet market; time alone would let a burst
    # build an unbounded batch.
    flush_max_rows: int = Field(default=500, alias="FX_FLUSH_MAX_ROWS")
    flush_max_seconds: float = Field(default=2.0, alias="FX_FLUSH_MAX_SECONDS")

    # How long an entry must sit unacknowledged before another worker adopts it.
    # Too low and a merely-slow worker gets its work stolen and duplicated; too
    # high and a genuinely dead worker's backlog sits idle. 60s is comfortably
    # above the flush interval.
    claim_idle_ms: int = Field(default=60_000, alias="FX_CLAIM_IDLE_MS")

    bucket_seconds: int = Field(default=60, alias="FX_BUCKET_SECONDS")
    consumer_name: str = Field(default_factory=socket.gethostname, alias="FX_CONSUMER_NAME")
    metrics_port: int = Field(default=9101, alias="FX_WORKER_METRICS_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
