"""Persistable solar production profile; never used to dispatch the battery.

Mirrors the household demand learning: a per-quarter (15-minute) mean PV power
profile is learned from the last HISTORY_DAYS days, rebuilt from recorder
statistics at startup so no blind learning period is needed. On top of the
raw profile an on-the-fly weather correction is applied: every quarter the
average measured power of the trailing hour (the last four fully elapsed
quarters) is compared with the profiled average for the same window, and the
ratio is applied to the remaining quarters of *today* only (never tomorrow).
The ratio is clamped to [0, MAX_HOUR_RATIO] and is only trusted when both
averages exceed MIN_HOUR_AVG_W, so tiny morning numbers cannot blow it up.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta, tzinfo
from statistics import mean

_LOGGER = logging.getLogger(__name__)

HISTORY_DAYS = 7
MIN_PROFILE_DAYS = 3
# A quarter is learned when at least this many 5-minute buckets were seen.
MIN_QUARTER_BUCKETS = 2
# Ratios above this multiple of the profile are clamped.
MAX_HOUR_RATIO = 3.0
# The trailing-hour correction is only trusted when both the measured and the
# profiled average power exceed this (W); below it the numbers are too small.
MIN_HOUR_AVG_W = 50.0
# The correction window: the last fully elapsed quarters (one hour).
RATIO_QUARTERS = 4
STORE_VERSION = 1


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _slot_of(local: datetime) -> str:
    return str(local.hour * 4 + local.minute // 15)


class SolarHistory:
    """Learn per-quarter PV power means (W) and forecast the next 24 hours."""

    def __init__(self) -> None:
        self.days: dict[str, dict[str, float]] = {}
        self._quarter: datetime | None = None
        self._wh = 0.0
        self._buckets = 0
        self._last: tuple[datetime, float] | None = None
        # (quarter start UTC, measured Wh, local date, local slot) of recent quarters.
        self._recent_quarters: list[tuple[datetime, float, str, str]] = []

    # -- persistence ------------------------------------------------------

    def as_dict(self) -> dict:
        return {"version": STORE_VERSION, "days": self.days}

    def restore(self, raw: object) -> None:
        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            return
        days = raw.get("days")
        if not isinstance(days, dict):
            return
        clean: dict[str, dict[str, float]] = {}
        for day, slots in days.items():
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            if not isinstance(slots, dict):
                continue
            entries: dict[str, float] = {}
            for slot, value in slots.items():
                number = _finite(value)
                if (
                    isinstance(slot, str)
                    and slot.isdigit()
                    and 0 <= int(slot) < 96
                    and number is not None
                    and number >= 0
                ):
                    entries[slot] = number
            if entries:
                clean[day] = entries
        self.days = clean

    # -- learning ---------------------------------------------------------

    def _prune(self, now: datetime, timezone: tzinfo) -> None:
        cutoff = (now.astimezone(timezone).date() - timedelta(days=HISTORY_DAYS + 2)).isoformat()
        self.days = {day: slots for day, slots in self.days.items() if day >= cutoff}

    def _flush_quarter(self, timezone: tzinfo) -> None:
        if self._quarter is None or self._buckets < MIN_QUARTER_BUCKETS:
            return
        local = self._quarter.astimezone(timezone)
        slot = _slot_of(local)
        day = local.date().isoformat()
        # _wh holds energy since the first bucket of the quarter; normalise to W.
        seconds = self._buckets * 300.0
        if seconds <= 0:
            return
        self.days.setdefault(day, {})[slot] = self._wh * 3600 / seconds
        self._recent_quarters.append((self._quarter, self._wh, day, slot))

    def _process(self, now: datetime, pv_w: float | None, timezone: tzinfo) -> None:
        now = now.astimezone(UTC)
        quarter = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
        if quarter != self._quarter:
            self._flush_quarter(timezone)
            self._quarter = quarter
            self._wh = 0.0
            self._buckets = 0
            self._last = None
        self._prune(now, timezone)
        cutoff = now - timedelta(minutes=15 * (RATIO_QUARTERS + 1))
        self._recent_quarters = [entry for entry in self._recent_quarters if entry[0] >= cutoff]
        value = _finite(pv_w)
        if value is None or value < 0:
            self._last = None
            return
        if self._last is not None:
            elapsed = (now - self._last[0]).total_seconds()
            if 0 < elapsed <= 720:
                trap = (self._last[1] + value) / 2
                self._wh += trap * elapsed / 3600
                self._buckets += 1
        self._last = (now, value)

    def observe(self, now: datetime, pv_w: float | None, timezone: tzinfo) -> None:
        """Accumulate one live PV sample (W)."""
        self._process(now, pv_w, timezone)

    def backfill(self, series: list[tuple[datetime, float]], timezone: tzinfo) -> None:
        """Rebuild the retained window from 5-minute recorder means (W)."""
        for stamp, value in sorted(series):
            self._process(stamp.astimezone(UTC), value, timezone)

    # -- forecast ---------------------------------------------------------

    def _profile_slot(self, slot: str, today: datetime.date) -> list[float]:
        values = []
        for offset in range(1, HISTORY_DAYS + 1):
            day = (today - timedelta(days=offset)).isoformat()
            value = self.days.get(day, {}).get(slot)
            if value is not None:
                values.append(value)
        return values

    def _ratio_window(self, now: datetime, timezone: tzinfo) -> tuple[float, float, float]:
        """Weather ratio over the trailing hour, evaluated per quarter.

        The window is the last RATIO_QUARTERS fully elapsed quarters: the
        measured average power (W) over that hour versus the profiled average
        for the same quarters. Returns (ratio, measured_avg_w, profiled_avg_w);
        the ratio is 1.0 unless both averages exceed MIN_HOUR_AVG_W, and it is
        clamped to [0, MAX_HOUR_RATIO].
        """
        now_utc = now.astimezone(UTC)
        current_quarter = now_utc.replace(
            minute=now_utc.minute // 15 * 15, second=0, microsecond=0
        )
        window_start = current_quarter - timedelta(minutes=15 * RATIO_QUARTERS)
        entries = [
            entry
            for entry in self._recent_quarters
            if window_start <= entry[0] < current_quarter
        ]
        if len(entries) != RATIO_QUARTERS:
            return 1.0, 0.0, 0.0
        for previous, current in zip(entries, entries[1:], strict=False):
            if current[0] - previous[0] != timedelta(minutes=15):
                return 1.0, 0.0, 0.0
        # Four quarters span exactly one hour, so Wh over the window == avg W.
        measured_avg_w = sum(entry[1] for entry in entries)
        baselines = []
        for _, _, day, slot in entries:
            values = self._profile_slot(slot, datetime.fromisoformat(day).date())
            if len(values) < MIN_PROFILE_DAYS:
                return 1.0, measured_avg_w, 0.0
            baselines.append(mean(values))
        profiled_avg_w = mean(baselines)
        if measured_avg_w <= MIN_HOUR_AVG_W or profiled_avg_w <= MIN_HOUR_AVG_W:
            return 1.0, measured_avg_w, profiled_avg_w
        ratio = measured_avg_w / profiled_avg_w
        if not math.isfinite(ratio):
            return 1.0, measured_avg_w, profiled_avg_w
        return min(max(ratio, 0.0), MAX_HOUR_RATIO), measured_avg_w, profiled_avg_w

    def hour_ratio(self, now: datetime, timezone: tzinfo) -> float:
        """Measured vs profiled average power of the trailing hour."""
        return self._ratio_window(now, timezone)[0]

    def forecast(self, now: datetime, timezone: tzinfo) -> dict:
        """Return 96 future quarter slots in W; today's tail is weather-corrected."""
        now = now.astimezone(UTC)
        first = now.replace(second=0, microsecond=0)
        first += timedelta(minutes=(-first.minute) % 15)
        if first <= now:
            first += timedelta(minutes=15)
        today_local = now.astimezone(timezone).date()
        ratio, measured_avg_w, profiled_avg_w = self._ratio_window(now, timezone)
        slots = []
        total_wh = 0.0
        for index in range(96):
            start = first + timedelta(minutes=15 * index)
            local = start.astimezone(timezone)
            slot = _slot_of(local)
            values = self._profile_slot(slot, today_local)
            baseline = mean(values) if len(values) >= MIN_PROFILE_DAYS else None
            is_today = local.date() == today_local
            watts = None
            if baseline is not None:
                watts = baseline * (ratio if is_today else 1.0)
                total_wh += watts * 0.25
            slots.append(
                {
                    "start": start.isoformat(),
                    "pv_w": round(watts, 1) if watts is not None else None,
                    "baseline_w": round(baseline, 1) if baseline is not None else None,
                }
            )
        learned_days = len(self.days)
        return {
            "status": "ready" if learned_days >= MIN_PROFILE_DAYS else "learning",
            "history_days": learned_days,
            "hour_ratio": round(ratio, 3),
            "ratio_measured_avg_w": round(measured_avg_w, 1),
            "ratio_profiled_avg_w": round(profiled_avg_w, 1),
            "slots": slots,
            "total_kwh": (
                round(total_wh / 1000, 3)
                if any(s["pv_w"] is not None for s in slots)
                else None
            ),
        }
