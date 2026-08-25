"""Prometheus exposition for the non-HTTP services.

The API already serves ``/metrics`` through FastAPI. The ingestor and worker have
no HTTP server of their own, so they start this tiny one. Without it those two -
the processes where the interesting failures actually happen - would be invisible
on the dashboard.
"""

from __future__ import annotations

import structlog
from prometheus_client import start_http_server

log = structlog.get_logger(__name__)


def serve_metrics(port: int) -> None:
    start_http_server(port)
    log.info("metrics.serving", port=port, path="/metrics")
