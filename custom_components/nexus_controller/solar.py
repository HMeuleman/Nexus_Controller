"""Solar forecast helpers: expected surplus for the battery and daytime load tracking."""

from __future__ import annotations

import math
from datetime import datetime

# Time constant of the daytime load average, in seconds of daylight (about three days).
LOAD_AVERAGE_TAU_S = 3 * 10 * 3600
# PV output above which a sample counts as daytime.
DAYTIME_PV_W = 50.0


def update_load_average(
    average_w: float | None, *, load_w: float, pv_power_w: float, dt_s: float
) -> float | None:
    """Fold a house-load sample into the daytime average; night samples are ignored."""
    if pv_power_w < DAYTIME_PV_W or not math.isfinite(load_w) or dt_s <= 0:
        return average_w
    load_w = max(load_w, 0.0)
    if average_w is None:
        return load_w
    weight = 1 - math.exp(-dt_s / LOAD_AVERAGE_TAU_S)
    return average_w + weight * (load_w - average_w)


def daylight_hours(
    *,
    above_horizon: bool,
    next_rising: datetime | None,
    next_setting: datetime | None,
    now: datetime,
) -> float | None:
    """Return the length of the remaining (or, at night, the upcoming) daylight period."""
    if next_setting is None:
        return None
    if above_horizon:
        return max((next_setting - now).total_seconds(), 0.0) / 3600
    if next_rising is None or next_setting <= next_rising:
        return None
    return (next_setting - next_rising).total_seconds() / 3600


def solar_surplus_kwh(
    forecast_kwh: float | None, average_load_w: float | None, daylight_h: float | None
) -> float | None:
    """Return the PV energy expected to be left over after the house has used its share."""
    if forecast_kwh is None or average_load_w is None or daylight_h is None:
        return None
    return max(forecast_kwh - average_load_w / 1000 * daylight_h, 0.0)
