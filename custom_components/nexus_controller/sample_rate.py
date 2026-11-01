"""Estimate how often the grid meter reports, so control timing can adapt to it."""

from __future__ import annotations

from statistics import median

MIN_INTERVAL_S = 0.5
MAX_INTERVAL_S = 60.0
WINDOW = 20
# Reports closer together than this belong to the same burst (one meter, several phase sensors).
BURST_S = 0.2


class SampleIntervalEstimator:
    """Median spacing of the most recent grid sample times (seconds)."""

    def __init__(self, default_s: float) -> None:
        self._default_s = default_s
        self._last: float | None = None
        self._gaps: list[float] = []

    def observe(self, timestamp_s: float) -> bool:
        """Record the arrival time of a grid sample; False when it is part of a burst."""
        if self._last is not None:
            gap = timestamp_s - self._last
            if gap < BURST_S:
                return False
            if gap <= MAX_INTERVAL_S * 3:
                self._gaps = [*self._gaps, gap][-WINDOW:]
        self._last = timestamp_s
        return True

    @property
    def interval_s(self) -> float:
        """Detected interval, or the default until at least three gaps were seen."""
        if len(self._gaps) < 3:
            return self._default_s
        return min(max(median(self._gaps), MIN_INTERVAL_S), MAX_INTERVAL_S)
