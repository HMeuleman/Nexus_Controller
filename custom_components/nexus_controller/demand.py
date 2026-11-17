"""Persistable household demand estimates; never used to dispatch the battery.

The heat pump is modelled purely from measured data: each local quarter keeps
its own seven-day mean profile (so a morning ramp or a night off is learned,
not assumed), and the forecast applies a per-quarter outdoor-temperature
correction fitted from the retained days. The seven-day window can be rebuilt
from Home Assistant recorder statistics at startup, so no blind learning
period is required after configuration.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import UTC, datetime, timedelta, tzinfo
from statistics import mean

HISTORY_DAYS = 7
MIN_PROFILE_DAYS = 3
MAX_SAMPLE_GAP_S = 120.0
# Backfill buckets are 5-minute statistics means; tolerate one lost bucket.
BACKFILL_MAX_GAP_S = 720.0
MIN_COVERAGE_S = 810.0
# Coverage actually measured between samples; held-value extension may bridge the tail.
MIN_MEASURED_S = 450.0
# The per-quarter temperature response is clamped to a sane multiple of the
# learned mean so a noisy slope cannot invent absurd demand.
WP_CORRECTION_MAX_FACTOR = 3.0
# Below this temperature variance (deg C^2) the slope is noise; no correction.
WP_TEMP_VAR_MIN = 0.25
STORE_VERSION = 2


def finite_number(value: object) -> float | None:
    """Accept only finite numeric values, including numeric state strings."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _non_negative(value: object) -> float | None:
    number = finite_number(value)
    return number if number is not None and number >= 0 else None


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


def normalize_temperature(value: object, unit: str) -> float | None:
    """Normalize a current temperature reading to Celsius."""
    if unit not in ("°C", "°F"):
        return None
    number = finite_number(value)
    if number is None:
        return None
    return (number - 32) / 1.8 if unit == "°F" else number


def temperature_at(temperatures: list[tuple[datetime, float]], when: datetime) -> float | None:
    """Interpolate the fetched temperature forecast at `when`; None outside or across gaps."""
    if not temperatures:
        return None
    times = [stamp.timestamp() for stamp, _ in temperatures]
    position = bisect_right(times, when.timestamp())
    if 0 < position < len(times) and times[position] - times[position - 1] <= 3600:
        fraction = (when.timestamp() - times[position - 1]) / (
            times[position] - times[position - 1]
        )
        return temperatures[position - 1][1] + fraction * (
            temperatures[position][1] - temperatures[position - 1][1]
        )
    return None


def _advance(last: tuple[datetime, float] | None, value: float | None, now: datetime):
    """Return (new_last, interval_seconds, trapezoid_mean); no interval when unusable."""
    if value is None:
        return None, 0.0, None
    if last is None:
        return (now, value), 0.0, None
    elapsed = (now - last[0]).total_seconds()
    if elapsed <= 0:
        return (now, value), 0.0, None
    return (now, value), elapsed, (last[1] + value) / 2


class DemandHistory:
    """Learn per-quarter house, heat-pump and outdoor temperature means (W, °C)."""

    def __init__(self) -> None:
        self.days: dict[str, dict[str, float]] = {}
        self.wp_days: dict[str, dict[str, float]] = {}
        self.temp_days: dict[str, dict[str, float]] = {}
        self.wp_configured = False
        self.rejected_quarters = 0
        self._quarter: datetime | None = None
        self._last_observed: datetime | None = None
        self._house_wh = 0.0
        self._wp_wh = 0.0
        self._temp_deg_s = 0.0
        self._house_cov = 0.0
        self._wp_cov = 0.0
        self._temp_cov = 0.0
        self._last_house: tuple[datetime, float] | None = None
        self._last_wp: tuple[datetime, float] | None = None
        self._last_temp: tuple[datetime, float] | None = None

    def as_dict(self) -> dict:
        """Persist complete quarters only; never bridge an integration restart."""
        return {
            "version": STORE_VERSION,
            "days": self.days,
            "wp_days": self.wp_days,
            "temp_days": self.temp_days,
            "rejected_quarters": self.rejected_quarters,
        }

    @staticmethod
    def _clean_profile(raw: object, non_negative: bool) -> dict[str, dict[str, float]]:
        clean: dict[str, dict[str, float]] = {}
        if not isinstance(raw, dict):
            return clean
        for day, slots in raw.items():
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            if not isinstance(slots, dict):
                continue
            entries = {}
            for slot, value in slots.items():
                number = finite_number(value)
                if isinstance(slot, str) and slot.isdigit() and 0 <= int(slot) < 96:
                    if number is not None and (not non_negative or number >= 0):
                        entries[str(int(slot))] = number
            clean[day] = entries
        return clean

    def restore(self, raw: object) -> None:
        """Reject corrupt, foreign-version or foreign-unit stored values."""
        self.days = {}
        self.wp_days = {}
        self.temp_days = {}
        self.rejected_quarters = 0
        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            return
        self.days = self._clean_profile(raw.get("days"), non_negative=True)
        self.wp_days = self._clean_profile(raw.get("wp_days"), non_negative=True)
        self.temp_days = self._clean_profile(raw.get("temp_days"), non_negative=False)
        rejected = raw.get("rejected_quarters")
        if isinstance(rejected, int) and rejected >= 0:
            self.rejected_quarters = rejected

    def _prune(self, now: datetime, timezone: tzinfo) -> None:
        cutoff = (now.astimezone(timezone).date() - timedelta(days=HISTORY_DAYS)).isoformat()
        self.days = {day: slots for day, slots in self.days.items() if day >= cutoff}
        self.wp_days = {day: slots for day, slots in self.wp_days.items() if day >= cutoff}
        self.temp_days = {day: slots for day, slots in self.temp_days.items() if day >= cutoff}

    def _flush_quarter(self, timezone: tzinfo) -> None:
        """Close the quarter: hold the last fresh sample over the tail, then grade.

        Extension never exceeds the seconds without a sample, so only genuinely
        sparse statistics cadences are padded and a stream that died mid-quarter
        fails the measured-coverage gate instead.
        """
        if self._quarter is None:
            return
        quarter_end = self._quarter + timedelta(minutes=15)
        house_measured, wp_measured, temp_measured = (
            self._house_cov,
            self._wp_cov,
            self._temp_cov,
        )
        extensions: list[tuple[tuple[datetime, float] | None, str, float]] = [
            (self._last_house, "house", house_measured),
            (self._last_wp, "wp", wp_measured),
            (self._last_temp, "temp", temp_measured),
        ]
        for last, stream, measured in extensions:
            if last is None:
                continue
            since_last = (quarter_end - last[0]).total_seconds()
            if since_last <= 0 or since_last > BACKFILL_MAX_GAP_S:
                continue
            seconds = min(since_last, 900.0 - measured)
            if seconds <= 0:
                continue
            if stream == "house":
                self._house_wh += last[1] * seconds / 3600
                self._house_cov += seconds
            elif stream == "wp":
                self._wp_wh += last[1] * seconds / 3600
                self._wp_cov += seconds
            else:
                self._temp_deg_s += last[1] * seconds
                self._temp_cov += seconds
        local = self._quarter.astimezone(timezone)
        unambiguous = local.utcoffset() == local.replace(fold=1 - local.fold).utcoffset()
        house_w = self._house_wh * 3600 / self._house_cov if self._house_cov else None
        wp_w = self._wp_wh * 3600 / self._wp_cov if self._wp_cov else None
        if (
            not unambiguous
            or house_w is None
            or house_measured < MIN_MEASURED_S
            or self._house_cov < MIN_COVERAGE_S
            or (
                self.wp_configured
                and (
                    wp_w is None
                    or wp_measured < MIN_MEASURED_S
                    or self._wp_cov < MIN_COVERAGE_S
                )
            )
        ):
            self.rejected_quarters += 1
            return
        slot = str(local.hour * 4 + local.minute // 15)
        day = local.date().isoformat()
        if wp_w is None:
            self.days.setdefault(day, {})[slot] = house_w
        elif house_w >= wp_w:
            self.days.setdefault(day, {})[slot] = house_w - wp_w
            self.wp_days.setdefault(day, {})[slot] = wp_w
        else:
            self.rejected_quarters += 1
            return
        if self._temp_cov > 0:
            self.temp_days.setdefault(day, {})[slot] = self._temp_deg_s / self._temp_cov

    def _process(
        self,
        now: datetime,
        house_w: float | None,
        wp_w: float | None,
        temp_c: float | None,
        timezone: tzinfo,
        max_gap_s: float,
    ) -> None:
        """Accumulate one sample set on the shared quarter timeline."""
        now = now.astimezone(UTC)
        if self._last_observed is not None and now <= self._last_observed:
            return
        self._last_observed = now
        quarter = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
        if quarter != self._quarter:
            self._flush_quarter(timezone)
            self._quarter = quarter
            self._house_wh = self._wp_wh = self._temp_deg_s = 0.0
            self._house_cov = self._wp_cov = self._temp_cov = 0.0
            self._last_house = self._last_wp = self._last_temp = None
        self._prune(now, timezone)
        house = _non_negative(house_w)
        wp = _non_negative(wp_w)
        temp = finite_number(temp_c)

        new_house, house_dt, house_trap = _advance(self._last_house, house, now)
        if house_dt and house_dt <= max_gap_s and house_trap is not None:
            self._house_wh += house_trap * house_dt / 3600
            self._house_cov += house_dt
        self._last_house = new_house

        new_wp, wp_dt, wp_trap = _advance(self._last_wp, wp, now)
        if wp_dt and wp_dt <= max_gap_s and wp_trap is not None:
            self._wp_wh += wp_trap * wp_dt / 3600
            self._wp_cov += wp_dt
        self._last_wp = new_wp

        new_temp, temp_dt, temp_trap = _advance(self._last_temp, temp, now)
        if temp_dt and temp_dt <= max_gap_s and temp_trap is not None:
            self._temp_deg_s += temp_trap * temp_dt
            self._temp_cov += temp_dt
        self._last_temp = new_temp

    def observe(
        self,
        now: datetime,
        house_w: float | None,
        wp_w: float | None,
        temp_c: float | None,
        timezone: tzinfo,
    ) -> None:
        """Accumulate one live sample set; unusable values just end their stream."""
        self._process(now, house_w, wp_w, temp_c, timezone, MAX_SAMPLE_GAP_S)

    def backfill(
        self,
        house: list[tuple[datetime, float | None]],
        wp: list[tuple[datetime, float | None]],
        temp: list[tuple[datetime, float | None]],
        timezone: tzinfo,
    ) -> None:
        """Rebuild the retained window from recorded five-minute means.

        All series ride one merged timeline of bucket ends; a series without a
        value at a bucket simply breaks its own stream, exactly like a live gap.
        """
        ends = sorted({t for t, _ in house} | {t for t, _ in wp} | {t for t, _ in temp})
        house_map, wp_map, temp_map = dict(house), dict(wp), dict(temp)
        for end in ends:
            self._process(
                end,
                house_map.get(end),
                wp_map.get(end),
                temp_map.get(end),
                timezone,
                BACKFILL_MAX_GAP_S,
            )

    def _wp_forecast(self, slot_key: str, day: str, predicted_temp: float | None) -> float | None:
        """Mean heat-pump power for a slot, corrected for the predicted temperature."""
        today = datetime.strptime(day, "%Y-%m-%d").date()
        values = []
        pairs: list[tuple[float, float]] = []
        for offset in range(1, HISTORY_DAYS + 1):
            date = (today - timedelta(days=offset)).isoformat()
            wp = self.wp_days.get(date, {}).get(slot_key)
            if wp is None:
                continue
            values.append(wp)
            temperature = self.temp_days.get(date, {}).get(slot_key)
            if temperature is not None:
                pairs.append((temperature, wp))
        if not values:
            return None
        mean_wp = mean(values)
        if predicted_temp is None or len(pairs) < MIN_PROFILE_DAYS:
            return mean_wp
        temps = [pair[0] for pair in pairs]
        mean_t = mean(temps)
        variance = sum((t - mean_t) ** 2 for t in temps) / len(temps)
        if variance < WP_TEMP_VAR_MIN:
            return mean_wp
        slope = sum((t - mean_t) * (w - mean_wp) for t, w in pairs) / (variance * len(pairs))
        predicted = mean_wp + slope * (predicted_temp - mean_t)
        if mean_wp > 0:
            return min(max(predicted, 0.0), WP_CORRECTION_MAX_FACTOR * mean_wp)
        return max(predicted, 0.0)

    def forecast(
        self,
        now: datetime,
        temperatures: list[tuple[datetime, float]],
        timezone: tzinfo,
        house_only: bool = False,
    ) -> dict:
        """Return 96 future quarter-hour points; absent coverage stays null."""
        first = now.astimezone(UTC).replace(second=0, microsecond=0)
        first += timedelta(minutes=(-first.minute) % 15)
        if first < now:
            first += timedelta(minutes=15)
        today = now.astimezone(timezone).date()
        day = today.isoformat()
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
            baseline = mean(values) if len(values) >= MIN_PROFILE_DAYS else None
            midpoint = start + timedelta(minutes=7, seconds=30)
            temperature = temperature_at(temperatures, midpoint)
            heat = None if house_only else self._wp_forecast(key, day, temperature)
            if baseline is None:
                total = None
            elif house_only:
                total = baseline
            elif heat is None:
                total = None
            else:
                total = baseline + heat
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
        heat_complete = not house_only and all(slot["heatpump_w"] is not None for slot in slots)
        return {
            "diagnostic_only": True,
            "status": "ready" if complete else "partial",
            "generated_at": now.isoformat(),
            "minimum_history_days": MIN_PROFILE_DAYS,
            "rejected_quarters": self.rejected_quarters,
            "heatpump_configured": not house_only,
            "household_kwh": round(sum(slot["total_w"] for slot in slots) / 4000, 3)
            if complete
            else None,
            "heatpump_kwh": round(sum(slot["heatpump_w"] for slot in slots) / 4000, 3)
            if heat_complete
            else None,
            "slots": slots,
        }
