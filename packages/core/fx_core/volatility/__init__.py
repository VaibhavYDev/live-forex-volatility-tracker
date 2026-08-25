"""Volatility estimators.

Three layers, each solving a different constraint:

* ``welford``          - O(1) numerically stable running variance.
* ``buckets``          - additive sufficient statistics, so sliding windows are
                         O(bars) instead of O(ticks).
* ``ewma``             - O(1) with no window at all; the headline number.
* ``range_estimators`` - Parkinson / Garman-Klass / Rogers-Satchell / Yang-Zhang,
                         which use the high and low that close-to-close discards.
"""

from fx_core.volatility.buckets import BucketAccumulator, RollingWindow
from fx_core.volatility.ewma import EwmaVariance, lambda_for_halflife
from fx_core.volatility.range_estimators import (
    close_to_close,
    garman_klass,
    parkinson,
    rogers_satchell,
    yang_zhang,
)
from fx_core.volatility.welford import Welford

__all__ = [
    "BucketAccumulator",
    "EwmaVariance",
    "RollingWindow",
    "Welford",
    "close_to_close",
    "garman_klass",
    "lambda_for_halflife",
    "parkinson",
    "rogers_satchell",
    "yang_zhang",
]
