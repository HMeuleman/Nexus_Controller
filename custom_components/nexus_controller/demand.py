"""Persistable household demand estimates; never used to dispatch the battery."""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import UTC, datetime, timedelta, tzinfo
from statistics import mean, median

HISTORY_DAYS = 7
MIN_PROFILE_DAYS = 3
MAX_SAMPLE_GAP_S = 120.0
MIN_COVERAGE_S = 810.0


def finite_number(value: object) -> float | None:
    """Accept only finite numeric values, including numeric state strings."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def parse_temperatures(forecast: object, unit: str) -> list[tuple[datetime, float]]:
    """Read Home Assistant hourly weather responses, normalizing to Celsius."""
    if not isinstance(forecast, list) or unit not in ("°C", "°F"):
        return []
    points = {}
    for row in forecast:
        if not isinstance(row, dict):
            continue
        temperature = finite_number(row.get("temperature"))
        try:
            start = datetime.fromisoformat(str(row.get("datetime")))
        except ValueError:
            continue
        if start.tzinfo is None or temperature is None:
            continue
        points[start.astimezone(UTC)] = (temperature - 32) / 1.8 if unit == "°F" else temperature
    return sorted(points.items())


def heatpump_power(temperature_c: float, local_time: datetime) -> float:
    """Provisional electrical watts fitted to supplied September-November history."""
    if not 6 <= local_time.hour < 23:
        return 0.0
    return 6.253 + 45.759 * max(0.0, 18 - temperature_c) + 168.532 * max(0.0, 8 - temperature_c)


class DemandHistory:
    """Learn non-heat-pump energy per local quarter from a daily kWh counter."""

    def __init__(self) -> None:
        self.days: dict[str, dict[str, float]] = {}
        self._quarter: datetime | None = None
        self._samples: list[tuple[datetime, float]] = []
        self._last: tuple[datetime, float] | None = None
        self._last_observed: datetime | None = None
        self._heat_kwh = 0.0
        self._coverage_s = 0.0
        self.rejected_quarters = 0

    def as_dict(self) -> dict:
        """Persist complete quarters only; never bridge an integration restart."""
        return {"days": self.days, "rejected_quarters": self.rejected_quarters}

    def restore(self, raw: object) -> None:
        """Reject corrupt stored values without preventing integration startup."""
        self.days = {}
        if not isinstance(raw, dict) or not isinstance(raw.get("days"), dict):
            return
        for day, slots in raw["days"].items():
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            if not isinstance(slots, dict):
                continue
            clean = {}
            for slot, energy in slots.items():
                value = finite_number(energy)
                if isinstance(slot, str) and slot.isdigit() and 0 <= int(slot) < 96:
                    if value is not None and value >= 0:
                        clean[str(int(slot))] = value
            self.days[day] = clean

    def _finish(self, timezone: tzinfo) -> None:
        if self._quarter is None:
            return
        local = self._quarter.astimezone(timezone)
        unambiguous = local.utcoffset() == local.replace(fold=1 - local.fold).utcoffset()
        first = [
            (stamp.timestamp(), value)
            for stamp, value in self._samples
            if (stamp - self._quarter).total_seconds() <= 60
        ]
        last = [
            (stamp.timestamp(), value)
            for stamp, value in self._samples
            if (stamp - self._quarter).total_seconds() >= 840
        ]
        if (
            unambiguous
            and len(first) >= 3
            and len(last) >= 3
            and self._coverage_s >= MIN_COVERAGE_S
        ):
            duration = median(stamp for stamp, _ in last) - median(stamp for stamp, _ in first)
            delta = median(value for _, value in last) - median(value for _, value in first)
            if duration > 0 and delta >= 0:
                house_kwh = delta * 900 / duration
                heat_kwh = self._heat_kwh * 900 / self._coverage_s
                if house_kwh >= heat_kwh:
                    slot = str(local.hour * 4 + local.minute // 15)
                    self.days.setdefault(local.date().isoformat(), {})[slot] = house_kwh - heat_kwh
                    return
        self.rejected_quarters += 1

    def observe(
        self, now: datetime, energy_kwh: float | None, heatpump_w: float | None, timezone: tzinfo
    ) -> None:
        """Accumulate bounded, time-weighted samples and ignore counter-reset spikes."""
        now = now.astimezone(UTC)
        if self._last_observed is not None and now <= self._last_observed:
            return
        self._last_observed = now
        quarter = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
        valid_power = finite_number(heatpump_w)
        if quarter != self._quarter:
            if self._last is not None and self._quarter is not None:
                remaining = (self._quarter + timedelta(minutes=15) - self._last[0]).total_seconds()
                if (
                    (now - self._last[0]).total_seconds() <= MAX_SAMPLE_GAP_S
                    and valid_power is not None
                    and valid_power >= 0
                ):
                    self._heat_kwh += self._last[1] * remaining / 3_600_000
                    self._coverage_s += remaining
            self._finish(timezone)
            self._quarter = quarter
            self._samples = []
            self._heat_kwh = self._coverage_s = 0.0
            self._last = None
        cutoff = (now.astimezone(timezone).date() - timedelta(days=HISTORY_DAYS)).isoformat()
        self.days = {day: slots for day, slots in self.days.items() if day >= cutoff}
        energy = finite_number(energy_kwh)
        if energy is not None and energy >= 0:
            self._samples.append((now, energy))
        if valid_power is None or valid_power < 0:
            self._last = None
            return
        if self._last is not None:
            elapsed = (now - self._last[0]).total_seconds()
            if 0 < elapsed <= MAX_SAMPLE_GAP_S:
                self._heat_kwh += (self._last[1] + valid_power) / 2 * elapsed / 3_600_000
                self._coverage_s += elapsed
        self._last = (now, valid_power)

    def forecast(
        self, now: datetime, temperatures: list[tuple[datetime, float]], timezone: tzinfo
    ) -> dict:
        """Return 96 future quarter-hour points; absent coverage stays null."""
        first = now.astimezone(UTC).replace(second=0, microsecond=0)
        first += timedelta(minutes=(-first.minute) % 15)
        if first < now:
            first += timedelta(minutes=15)
        today = now.astimezone(timezone).date()
        times = [stamp.timestamp() for stamp, _ in temperatures]
        slots = []
        for index in range(96):
            start = first + timedelta(minutes=15 * index)
            local = start.astimezone(timezone)
            key = str(local.hour * 4 + local.minute // 15)
            history = [
                self.days.get((today - timedelta(days=offset)).isoformat(), {}).get(key)
                for offset in range(1, HISTORY_DAYS + 1)
            ]
            values = [value for value in history if value is not None]
            baseline = mean(values) * 4000 if len(values) >= MIN_PROFILE_DAYS else None
            midpoint = start + timedelta(minutes=7, seconds=30)
            position = bisect_right(times, midpoint.timestamp())
            temperature = None
            if 0 < position < len(times) and times[position] - times[position - 1] <= 3600:
                fraction = (midpoint.timestamp() - times[position - 1]) / (
                    times[position] - times[position - 1]
                )
                temperature = temperatures[position - 1][1] + fraction * (
                    temperatures[position][1] - temperatures[position - 1][1]
                )
            heat = (
                heatpump_power(temperature, midpoint.astimezone(timezone))
                if temperature is not None
                else 0.0
                if not 6 <= local.hour < 23
                else None
            )
            total = baseline + heat if baseline is not None and heat is not None else None
            slots.append(
                {
                    "start": start.isoformat(),
                    "baseline_w": round(baseline, 1) if baseline is not None else None,
                    "heatpump_w": round(heat, 1) if heat is not None else None,
                    "total_w": round(total, 1) if total is not None else None,
                    "temperature_c": round(temperature, 2) if temperature is not None else None,
                    "history_days": len(values),
                    "temperature_extrapolated": temperature is not None
                    and not 1.1 <= temperature <= 27,
                }
            )
        complete = all(slot["total_w"] is not None for slot in slots)
        heat_complete = all(slot["heatpump_w"] is not None for slot in slots)
        return {
            "diagnostic_only": True,
            "status": "ready" if complete else "partial",
            "generated_at": now.isoformat(),
            "minimum_history_days": MIN_PROFILE_DAYS,
            "rejected_quarters": self.rejected_quarters,
            "household_kwh": round(sum(slot["total_w"] for slot in slots) / 4000, 3)
            if complete
            else None,
            "heatpump_kwh": round(sum(slot["heatpump_w"] for slot in slots) / 4000, 3)
            if heat_complete
            else None,
            "slots": slots,
        }
