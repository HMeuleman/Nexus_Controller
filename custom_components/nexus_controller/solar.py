"""Solar forecast helpers: expected surplus for the battery from demand-forecast slots."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta, tzinfo


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


def house_energy_kwh(
    slots: object, window_start: datetime, window_end: datetime
) -> float | None:
    """Predicted house energy in kWh within [window_start, window_end) from 15-min slots."""
    if not isinstance(slots, list) or window_end <= window_start:
        return None
    start_utc = window_start.astimezone(UTC)
    end_utc = window_end.astimezone(UTC)
    total = 0.0
    found = False
    for slot in slots:
        if not isinstance(slot, Mapping):
            continue
        raw_w = slot.get("total_w")
        if raw_w is None or isinstance(raw_w, bool):
            continue
        try:
            start = datetime.fromisoformat(str(slot["start"]))
            value = float(raw_w)
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is None or not math.isfinite(value) or value < 0:
            continue
        slot_start = start.astimezone(UTC)
        slot_end = slot_start + timedelta(minutes=15)
        overlap_s = (min(end_utc, slot_end) - max(start_utc, slot_start)).total_seconds()
        if overlap_s > 0:
            total += value * overlap_s / 3_600_000
            found = True
    return total if found else None


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
