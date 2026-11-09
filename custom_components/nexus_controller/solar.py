"""Solar forecast helpers: expected surplus for the battery and daytime load tracking."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta, tzinfo

# Time constant of the daytime load average, in seconds of daylight (about three days).
LOAD_AVERAGE_TAU_S = 3 * 10 * 3600
# PV output above which a sample counts as daytime.
DAYTIME_PV_W = 50.0


def hourly_forecast_quarters(
    hours: object, *, day: date, timezone: tzinfo
) -> dict[datetime, float]:
    """Convert local hourly kWh forecasts into UTC quarter-hour kWh estimates."""
    if not isinstance(hours, Mapping):
        return {}
    quarters: dict[datetime, float] = {}
    for label, raw_energy in hours.items():
        if not isinstance(label, str) or len(label) != 5 or label[2:] != ":00":
            continue
        try:
            hour = int(label[:2])
            if not 0 <= hour <= 23 or label[:2] != f"{hour:02d}":
                continue
            energy = float(raw_energy)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(energy) or energy < 0 or isinstance(raw_energy, bool):
            continue
        start = datetime.combine(day, time(hour), tzinfo=timezone)
        if start.utcoffset() != start.replace(fold=1).utcoffset():
            continue
        start_utc = start.astimezone(UTC)
        if start_utc.astimezone(timezone).replace(tzinfo=None) != start.replace(tzinfo=None):
            continue
        for quarter in range(4):
            quarters[start_utc + timedelta(minutes=15 * quarter)] = energy / 4
    return quarters


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
