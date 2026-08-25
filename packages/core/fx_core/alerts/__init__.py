"""Volatility regime detection.

Four pieces, deliberately separate because they answer different questions:

* ``seasonality`` - what does this hour of the day normally look like?
* ``baseline``    - what does "normal" look like for this pair, in log space?
* ``hysteresis``  - given a z-score, should we commit to a regime change?
* ``detector``    - composes the three, and owns the calendar/feed gates.

Everything here is pure: no I/O, no clock, no Redis. The ingestor supplies the
sigma and the timestamp and receives an optional ``RegimeTransition``.
"""

from fx_core.alerts.baseline import (
    BaselineSnapshot,
    VolatilityBaseline,
    lambda_for_halflife,
    robust_zscore,
)
from fx_core.alerts.detector import DetectorConfig, DetectorSnapshot, RegimeDetector
from fx_core.alerts.hysteresis import (
    Regime,
    RegimeTransition,
    SchmittTrigger,
    TransitionCause,
    TriggerConfig,
    TriggerSnapshot,
)
from fx_core.alerts.seasonality import DiurnalProfile, DiurnalSnapshot

__all__ = [
    "BaselineSnapshot",
    "DetectorConfig",
    "DetectorSnapshot",
    "DiurnalProfile",
    "DiurnalSnapshot",
    "Regime",
    "RegimeDetector",
    "RegimeTransition",
    "SchmittTrigger",
    "TransitionCause",
    "TriggerConfig",
    "TriggerSnapshot",
    "VolatilityBaseline",
    "lambda_for_halflife",
    "robust_zscore",
]
