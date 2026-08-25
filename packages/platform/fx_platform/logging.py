"""Structured JSON logging.

JSON rather than pretty text in production because these logs are meant to be
*queried*: "show me every reconnect for EURUSD in the last hour" is a filter, not
a grep. Console rendering stays available for local work, since JSON in a terminal
is genuinely unreadable and pretending otherwise just means nobody reads the logs.

Every service binds a ``service`` field at startup, so one aggregated stream stays
attributable. Correlation ids thread tick -> bar -> alert.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(service: str, level: str = "INFO", json_output: bool | None = None) -> None:
    if json_output is None:
        json_output = not sys.stderr.isatty()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)
