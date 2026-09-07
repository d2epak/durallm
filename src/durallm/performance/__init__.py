"""Native Performance and Accelerated Gateway Core (Frontier 7)."""

from durallm.performance.accelerator import (
    FastSlidingWindow,
    FastStreamRelay,
    FastTokenEstimator,
    get_accelerated_event_loop,
    is_uvloop_active,
)

__all__ = [
    "FastStreamRelay",
    "FastTokenEstimator",
    "FastSlidingWindow",
    "get_accelerated_event_loop",
    "is_uvloop_active",
]
