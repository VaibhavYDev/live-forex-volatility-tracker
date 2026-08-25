"""Shared infrastructure adapters for the three Python services.

Distinct from ``fx_core`` on purpose:

* ``fx_core``     - pure domain logic. Zero dependencies, zero I/O, instant tests.
* ``fx_platform`` - the boring adapters every service needs (logging, Redis, a
                    metrics endpoint). Has dependencies, does I/O, but no domain
                    knowledge whatsoever.

Keeping them apart is what stops "just one import" from dragging Redis into the
volatility maths and making the unit tests need a container.
"""

from fx_platform.logging import configure_logging
from fx_platform.metrics import serve_metrics
from fx_platform.redis_client import close_redis, make_redis

__all__ = ["close_redis", "configure_logging", "make_redis", "serve_metrics"]
