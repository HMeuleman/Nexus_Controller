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


def test_hour_ratio_corrects_only_todays_remaining_slots():
    history = SolarHistory()
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        history.backfill(_series(day, 10, 1000.0), UTC)
        history.backfill(_series(day, 11, 1000.0), UTC)
    # Now at 11:30 on the 19th, with a measured 500 W so far in hour 11
    # (profile says 1000 W) => ratio well below 1.
    now = datetime(2026, 11, 19, 11, 30, tzinfo=UTC)
    for minute in range(0, 31, 5):
        history.observe(now - timedelta(minutes=30 - minute), 500.0, UTC)
    result = history.forecast(now, UTC)
    assert 0.2 < result["hour_ratio"] < 0.7
    today_slot = next(s for s in result["slots"] if s["start"].startswith("2026-11-19T11:45"))
    tomorrow_slot = next(s for s in result["slots"] if s["start"].startswith("2026-11-20T11:30"))
    # Today's remaining hours get the ratio, tomorrow stays at the baseline.
    expected = today_slot["baseline_w"] * result["hour_ratio"]
    assert today_slot["pv_w"] == pytest.approx(expected, rel=0.05)
    assert today_slot["baseline_w"] == pytest.approx(1000.0, abs=60.0)
    assert today_slot["pv_w"] < today_slot["baseline_w"]
    assert tomorrow_slot["pv_w"] == pytest.approx(tomorrow_slot["baseline_w"])


def test_hour_ratio_is_clamped_and_defaults_to_one_at_night():
    history = SolarHistory()
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        history.backfill(_series(day, 10, 1000.0), UTC)
    night = datetime(2026, 11, 19, 2, 30, tzinfo=UTC)
    for minute in range(0, 30, 5):
        history.observe(night - timedelta(minutes=30 - minute), 0.0, UTC)
    assert history.hour_ratio(night, UTC) == 1.0
    day = datetime(2026, 11, 19, 10, 30, tzinfo=UTC)
    for minute in range(0, 30, 5):
        history.observe(day - timedelta(minutes=30 - minute), 5000.0, UTC)
    assert history.hour_ratio(day, UTC) == pytest.approx(MAX_HOUR_RATIO)


def test_quarter_with_too_few_buckets_is_not_learned():
    history = SolarHistory()
    base = datetime(2026, 11, 18, 10, tzinfo=UTC)
    history.backfill([(base, 3000.0)], UTC)  # single bucket only
    assert history.days == {}
