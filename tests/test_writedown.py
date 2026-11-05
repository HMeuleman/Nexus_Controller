"""Tests for the stored-value write-down."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.nexus_controller.tracker import BatteryTracker

START = datetime(2026, 11, 1, 12, tzinfo=UTC)
COMMON = {"grid_import_w": 0.0, "import_price": 0.30, "export_price": 0.20}


def _tracker(stored=0.60):
    tracker = BatteryTracker(20.8)
    tracker.state.stored_value = stored
    tracker.update(now=START, day="d", soc_pct=80, battery_power_w=0.0, **COMMON)
    tracker.write_down(now=START, soc_pct=80, forecast_prices=[])
    return tracker


def _step(tracker, hours, soc=80, power=0.0, forecast=()):
    now = START + timedelta(hours=hours)
    tracker.update(now=now, day="d", soc_pct=soc, battery_power_w=power, **COMMON)
    return tracker.write_down(now=now, soc_pct=soc, forecast_prices=list(forecast))


def test_nothing_changes_within_the_first_day():
    tracker = _tracker()

    assert not _step(tracker, 23)
    assert tracker.state.stored_value == 0.60


def test_value_drifts_towards_the_average_price_after_a_day_without_discharge():
    tracker = _tracker()
    assert _step(tracker, 24)  # first step of the episode, so the user is notified

    assert not _step(tracker, 24.1)
    for minute in range(7, 60 * 24, 10):
        _step(tracker, 24.1 + minute / 60)
    # A day of this closes about a third of the gap to the 0.30 average price.
    assert tracker.state.stored_value == pytest.approx(0.30 + 0.30 * 0.67, abs=0.01)


def test_nothing_happens_at_low_soc_or_below_the_market():
    low = _tracker()
    _step(low, 25, soc=40)
    _step(low, 26, soc=40)
    cheap = _tracker(stored=0.20)
    _step(cheap, 25)
    _step(cheap, 26)

    assert low.state.stored_value == 0.60
    assert cheap.state.stored_value == 0.20


def test_a_discharge_restarts_the_wait_and_stops_the_write_down():
    tracker = _tracker()
    _step(tracker, 25)
    _step(tracker, 26)
    lowered = tracker.state.stored_value
    assert lowered < 0.60

    _step(tracker, 26.1, power=800.0)
    _step(tracker, 40)
    _step(tracker, 41)

    assert tracker.state.stored_value == lowered
    assert not tracker.state.writedown_active


def test_a_high_forecast_keeps_the_value_up():
    tracker = _tracker(stored=0.60)
    _step(tracker, 25, forecast=[0.58, 0.60, 0.62, 0.64])
    _step(tracker, 26, forecast=[0.58, 0.60, 0.62, 0.64])

    assert tracker.state.stored_value == 0.60
