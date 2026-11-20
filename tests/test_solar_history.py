"""Tests for the learned solar production profile and its weather correction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.nexus_controller.solar_history import (
    MAX_HOUR_RATIO,
    SolarHistory,
)


def _series(day: str, start_hour: int, watts: float, buckets: int = 13) -> list:
    base = datetime.fromisoformat(f"{day}T{start_hour:02d}:00:00+00:00")
    return [(base + timedelta(minutes=5 * i), watts) for i in range(buckets)]


def test_backfill_learns_quarter_means_and_forecast_needs_min_days():
    history = SolarHistory()
    # Three full days of 2000 W between 10:00 and 13:00 UTC.
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        history.backfill(_series(day, 10, 2000.0), UTC)
    assert history.days["2026-11-18"]["40"] == pytest.approx(2000.0)
    result = history.forecast(datetime(2026, 11, 19, 9, tzinfo=UTC), UTC)
    assert result["status"] == "ready"
    assert result["history_days"] == 3
    slot = next(s for s in result["slots"] if s["start"].startswith("2026-11-19T10:00"))
    assert slot["pv_w"] == pytest.approx(2000.0)
    # 13 buckets span 10:00-11:00: four quarters learned, the last flushes on
    # the 11:00 sample, so all four slots carry the 2000 W baseline.
    assert result["total_kwh"] == pytest.approx(4 * 2000.0 * 0.25 / 1000, abs=0.01)


def test_forecast_without_history_stays_learning():
    history = SolarHistory()
    result = history.forecast(datetime(2026, 11, 19, 9, tzinfo=UTC), UTC)
    assert result["status"] == "learning"
    assert result["total_kwh"] is None
    assert all(s["pv_w"] is None for s in result["slots"])


def _observe_hour(history: SolarHistory, end: datetime, watts: float) -> None:
    """Feed 5-minute samples of `watts` W for the hour ending at `end`."""
    for minute in range(0, 61, 5):
        history.observe(end - timedelta(minutes=60 - minute), watts, UTC)


def test_hour_ratio_uses_trailing_hour_and_corrects_only_todays_remaining_slots():
    history = SolarHistory()
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        history.backfill(_series(day, 10, 1000.0), UTC)
        history.backfill(_series(day, 11, 1000.0), UTC)
    # Measured 500 W over the trailing hour 10:30-11:30 on the 19th
    # (profile says 1000 W) => ratio well below 1.
    now = datetime(2026, 11, 19, 11, 30, tzinfo=UTC)
    _observe_hour(history, now, 500.0)
    result = history.forecast(now, UTC)
    assert 0.2 < result["hour_ratio"] < 0.7
    # Each quarter integrates 2 x 5 min of samples, so 500 W measures as 333 W avg.
    assert result["ratio_measured_avg_w"] == pytest.approx(500.0 * 600 / 900, rel=0.05)
    assert result["ratio_profiled_avg_w"] == pytest.approx(1000.0)
    today_slot = next(s for s in result["slots"] if s["start"].startswith("2026-11-19T11:45"))
    tomorrow_slot = next(s for s in result["slots"] if s["start"].startswith("2026-11-20T11:30"))
    # Today's remaining quarters get the ratio, tomorrow stays at the baseline.
    expected = today_slot["baseline_w"] * result["hour_ratio"]
    assert today_slot["pv_w"] == pytest.approx(expected, rel=0.05)
    assert today_slot["baseline_w"] == pytest.approx(1000.0, abs=60.0)
    assert today_slot["pv_w"] < today_slot["baseline_w"]
    assert tomorrow_slot["pv_w"] == pytest.approx(tomorrow_slot["baseline_w"])


def test_hour_ratio_is_clamped_and_defaults_to_one_at_night():
    history = SolarHistory()
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        history.backfill(_series(day, 9, 1000.0), UTC)
        history.backfill(_series(day, 10, 1000.0), UTC)
    night = datetime(2026, 11, 19, 2, 30, tzinfo=UTC)
    _observe_hour(history, night, 0.0)
    assert history.hour_ratio(night, UTC) == 1.0
    day = datetime(2026, 11, 19, 10, 30, tzinfo=UTC)
    _observe_hour(history, day, 5000.0)
    assert history.hour_ratio(day, UTC) == pytest.approx(MAX_HOUR_RATIO)


def test_hour_ratio_ignores_current_hour_until_a_full_trailing_hour_elapsed():
    """Only the trailing hour counts; the current hour so far does not."""
    history = SolarHistory()
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        history.backfill(_series(day, 10, 1000.0), UTC)
        history.backfill(_series(day, 11, 1000.0), UTC)
    now = datetime(2026, 11, 19, 11, 45, tzinfo=UTC)
    # 500 W measured only during the current hour 11:00-11:45: just three
    # quarters flushed, no full trailing hour yet => no correction.
    for minute in range(0, 46, 5):
        history.observe(now - timedelta(minutes=45 - minute), 500.0, UTC)
    assert history.hour_ratio(now, UTC) == 1.0


def test_hour_ratio_requires_both_averages_above_50w():
    now = datetime(2026, 11, 19, 11, 30, tzinfo=UTC)
    # Profile below 50 W: no correction even though measured is high.
    low_profile = SolarHistory()
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        low_profile.backfill(_series(day, 10, 40.0), UTC)
        low_profile.backfill(_series(day, 11, 40.0), UTC)
    _observe_hour(low_profile, now, 500.0)
    assert low_profile.hour_ratio(now, UTC) == 1.0
    # Measured below 50 W: no correction even though the profile is high.
    low_measured = SolarHistory()
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        low_measured.backfill(_series(day, 10, 1000.0), UTC)
        low_measured.backfill(_series(day, 11, 1000.0), UTC)
    _observe_hour(low_measured, now, 30.0)
    assert low_measured.hour_ratio(now, UTC) == 1.0


def test_hour_ratio_updates_each_quarter():
    history = SolarHistory()
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        history.backfill(_series(day, 10, 1000.0), UTC)
        history.backfill(_series(day, 11, 1000.0), UTC)
        history.backfill(_series(day, 12, 1000.0), UTC)
    at_1130 = datetime(2026, 11, 19, 11, 30, tzinfo=UTC)
    _observe_hour(history, at_1130, 500.0)
    assert history.hour_ratio(at_1130, UTC) == pytest.approx(500.0 * 600 / 900 / 1000.0, rel=0.05)
    # The next hour is sunny: at 12:30 the trailing window is 11:30-12:30.
    at_1230 = datetime(2026, 11, 19, 12, 30, tzinfo=UTC)
    _observe_hour(history, at_1230, 2000.0)
    assert history.hour_ratio(at_1230, UTC) == pytest.approx(2000.0 * 600 / 900 / 1000.0, rel=0.05)


def test_quarter_with_too_few_buckets_is_not_learned():
    history = SolarHistory()
    base = datetime(2026, 11, 18, 10, tzinfo=UTC)
    history.backfill([(base, 3000.0)], UTC)  # single bucket only
    assert history.days == {}
