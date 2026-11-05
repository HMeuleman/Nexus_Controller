"""Tests for the configurable write-down parameters."""

from datetime import UTC, datetime, timedelta

from custom_components.nexus_controller.tracker import BatteryTracker

START = datetime(2026, 11, 1, 12, tzinfo=UTC)
COMMON = {"grid_import_w": 0.0, "import_price": 0.30, "export_price": 0.20}


def _run(**kwargs):
    tracker = BatteryTracker(20.8)
    tracker.state.stored_value = 0.60
    tracker.update(now=START, day="d", soc_pct=80, battery_power_w=0.0, **COMMON)
    tracker.write_down(now=START, soc_pct=80, forecast_prices=[], **kwargs)
    for hours in (25, 26):
        now = START + timedelta(hours=hours)
        tracker.update(now=now, day="d", soc_pct=80, battery_power_w=0.0, **COMMON)
        tracker.write_down(now=now, soc_pct=80, forecast_prices=[], **kwargs)
    return tracker.state.stored_value


def test_a_zero_rate_switches_the_write_down_off():
    assert _run(fraction_per_day=0.0) == 0.60


def test_the_wait_and_soc_limits_are_settable():
    assert _run(wait_h=48.0) == 0.60
    assert _run(min_soc_pct=90.0) == 0.60
    assert _run(wait_h=0.0) < 0.60
