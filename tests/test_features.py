"""Tests for efficiency-aware charge planning, tracking, solar and sample interval helpers."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.nexus_controller.charging import inverter_efficiency, plan_net_charge
from custom_components.nexus_controller.sample_rate import SampleIntervalEstimator
from custom_components.nexus_controller.solar import (
    daylight_hours,
    solar_surplus_kwh,
    update_load_average,
)
from custom_components.nexus_controller.tracker import BatteryTracker
from custom_components.nexus_controller.utils import parse_energy_kwh, parse_forecast_tax

PLAN = {
    "slot_hours": 1.0,
    "min_earnings_eur_kwh": 0.20,
    "max_power_w": 10000.0,
    "discharge_power_w": 1000.0,
    "min_power_w": 500.0,
}


def test_efficiency_follows_the_measured_curve():
    assert inverter_efficiency(500) == pytest.approx(0.875)
    assert inverter_efficiency(100) == pytest.approx(0.85)
    assert inverter_efficiency(1500) == pytest.approx(0.9175)
    assert inverter_efficiency(6000) == pytest.approx(0.975)
    assert inverter_efficiency(10000) == pytest.approx(0.9575)
    assert inverter_efficiency(500, pv_share=1.0) == pytest.approx(0.90)
    assert inverter_efficiency(-5000) == pytest.approx(0.975)


def test_charge_goes_to_the_efficient_mid_power_not_the_lowest():
    prices = [0.10, 0.10, 0.10, 0.60, 0.60, 0.60]

    plan = plan_net_charge(prices_eur_kwh=prices, headroom_kwh=10.0, **PLAN)

    assert plan.charge
    assert plan.slots == 2  # 10 kWh in 3 slots would be ~3.5 kW, 2 slots is ~5.1 kW and better
    assert 4800 < plan.power_w < 5400


def test_selling_to_the_grid_at_mid_power_makes_a_small_spread_pay():
    prices = [0.3486, 0.5577, 0.52, 0.50, 0.45]
    plan = {**PLAN, "discharge_power_w": 500.0, "min_earnings_eur_kwh": 0.15}

    house_only = plan_net_charge(prices_eur_kwh=prices, headroom_kwh=5.0, **plan)
    with_export = plan_net_charge(
        prices_eur_kwh=prices, headroom_kwh=5.0, export_tax_eur_kwh=0.0, **plan
    )

    assert not house_only.charge
    assert with_export.charge
    assert 4500 < with_export.power_w < 7600


def test_expensive_current_slot_does_not_charge():
    prices = [0.60, 0.10, 0.10, 0.10, 0.60]

    plan = plan_net_charge(prices_eur_kwh=prices, headroom_kwh=3.0, **PLAN)

    assert not plan.charge


def test_no_charge_without_enough_spread():
    prices = [0.20, 0.22, 0.25, 0.24]

    assert not plan_net_charge(prices_eur_kwh=prices, headroom_kwh=5.0, **PLAN).charge


def test_partial_fill_uses_full_power_in_the_profitable_slot_only():
    prices = [0.10, 0.50, 0.60, 0.70]

    plan = plan_net_charge(prices_eur_kwh=prices, headroom_kwh=15.0, **PLAN)

    assert plan.charge
    assert plan.power_w <= 10000


def test_unfinished_current_slot_makes_the_plan_use_more_slots():
    prices = [0.10, 0.10, 0.10, 0.60, 0.60, 0.60]
    full = plan_net_charge(prices_eur_kwh=prices, headroom_kwh=6.0, **PLAN)
    late = plan_net_charge(
        prices_eur_kwh=prices, headroom_kwh=6.0, first_slot_remaining=0.25, **PLAN
    )

    assert late.charge
    assert late.slots > full.slots


def test_tracker_counts_cycles_per_soc_gain_and_resets_daily():
    tracker = BatteryTracker(20.8)
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    common = {
        "battery_power_w": 0.0,
        "grid_import_w": 0.0,
        "import_price": 0.3,
        "export_price": 0.2,
    }
    tracker.update(now=now, day="d1", soc_pct=50, **common)
    tracker.update(now=now + timedelta(seconds=10), day="d1", soc_pct=51, **common)
    tracker.update(now=now + timedelta(seconds=20), day="d1", soc_pct=50, **common)

    assert tracker.state.cycles_today == pytest.approx(0.01)
    tracker.update(now=now + timedelta(seconds=30), day="d2", soc_pct=52, **common)
    assert tracker.state.cycles_today == pytest.approx(0.02)
    assert tracker.state.cycles_total == pytest.approx(0.03)


def test_tracker_earns_the_spread_between_charge_and_discharge_price():
    tracker = BatteryTracker(20.8)
    tracker.state.stored_value = 0.0
    start = datetime(2026, 11, 1, 2, tzinfo=UTC)
    # Charge 2 kW from the grid for an hour at 0.10 in 10 s steps.
    for step in range(361):
        tracker.update(
            now=start + timedelta(seconds=10 * step),
            day="d",
            soc_pct=2,
            battery_power_w=-2000.0,
            grid_import_w=2000.0,
            import_price=0.10,
            export_price=0.0,
        )
    assert 0.07 < tracker.state.stored_value < 0.11

    later = start + timedelta(hours=10)
    for step in range(361):
        tracker.update(
            now=later + timedelta(seconds=10 * step),
            day="d",
            soc_pct=40,
            battery_power_w=1000.0,
            grid_import_w=0.0,
            import_price=0.40,
            export_price=0.30,
        )
    assert tracker.state.earned_today > 0.05
    assert tracker.state.earned_total == tracker.state.earned_today


def test_tracker_ignores_long_gaps():
    tracker = BatteryTracker(20.8)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    common = {"soc_pct": 50, "grid_import_w": 0.0, "import_price": 0.4, "export_price": 0.3}
    tracker.update(now=now, day="d", battery_power_w=1000.0, **common)
    tracker.update(now=now + timedelta(hours=1), day="d", battery_power_w=1000.0, **common)

    assert tracker.state.earned_total == 0.0


def test_tracker_state_roundtrip_ignores_garbage():
    tracker = BatteryTracker(20.8)
    tracker.state.earned_total = 12.5
    restored = BatteryTracker.state_from_dict({**tracker.as_dict(), "bogus": 1})
    assert restored.earned_total == 12.5
    assert BatteryTracker.state_from_dict("nope").earned_total == 0.0


def test_sample_interval_is_detected_from_arrival_times():
    estimator = SampleIntervalEstimator(10.0)
    assert estimator.interval_s == 10.0
    for second in (0, 3, 6, 9, 12):
        estimator.observe(float(second))

    assert estimator.interval_s == pytest.approx(3.0)


def test_sample_interval_ignores_bursts_of_phase_updates():
    estimator = SampleIntervalEstimator(10.0)
    counted = [estimator.observe(t) for t in (0.0, 0.05, 10.0, 10.1, 20.0, 30.0)]

    assert counted == [True, False, True, False, True, True]
    assert estimator.interval_s == pytest.approx(10.0)


def test_load_average_only_learns_in_daylight():
    assert update_load_average(None, load_w=400, pv_power_w=0, dt_s=10) is None
    assert update_load_average(None, load_w=400, pv_power_w=1000, dt_s=10) == 400
    assert update_load_average(400, load_w=1400, pv_power_w=1000, dt_s=10) > 400


def test_solar_surplus_subtracts_expected_house_use():
    now = datetime(2026, 6, 1, 8, tzinfo=UTC)
    hours = daylight_hours(
        above_horizon=True,
        next_rising=now + timedelta(hours=16),
        next_setting=now + timedelta(hours=10),
        now=now,
    )

    assert hours == 10
    assert solar_surplus_kwh(20.0, 500.0, hours) == pytest.approx(15.0)
    assert solar_surplus_kwh(2.0, 500.0, hours) == 0.0
    assert solar_surplus_kwh(None, 500.0, hours) is None


def test_night_uses_the_length_of_the_coming_day():
    now = datetime(2026, 6, 1, 22, tzinfo=UTC)
    hours = daylight_hours(
        above_horizon=False,
        next_rising=now + timedelta(hours=6),
        next_setting=now + timedelta(hours=20),
        now=now,
    )

    assert hours == 14


def test_forecast_tax_comes_from_a_tax_free_price_or_explicit_tax():
    forecast = [
        {
            "start_date": "2026-11-01T10:00:00+00:00",
            "price_tax_included": {"amount": 3000000},
            "price_tax_excluded": {"amount": 1900000},
        },
        {
            "start_date": "2026-11-01T11:00:00+00:00",
            "price_tax_included": {"amount": 3000000},
            "tax": {"amount": 1100000},
        },
        {"start_date": "2026-11-01T12:00:00+00:00", "price_tax_included": {"amount": 3000000}},
    ]

    taxes = parse_forecast_tax(forecast)

    assert taxes == {
        datetime(2026, 11, 1, 10, tzinfo=UTC): pytest.approx(0.11),
        datetime(2026, 11, 1, 11, tzinfo=UTC): pytest.approx(0.11),
    }
    assert parse_forecast_tax(None) == {}


def test_forecast_tax_reads_the_live_zonneplan_quarter_entries():
    start = datetime(2026, 10, 31, 15, tzinfo=UTC)
    forecast = [
        {
            "start_date": start,
            "end_date": start + timedelta(minutes=15),
            "price_tax_included": {"amount": 4493189},
            "price_tax_excluded": {"amount": 3384708},
            "sustainability_score": {"permille": 127},
        }
    ]

    assert parse_forecast_tax(forecast)[start] == pytest.approx(0.1108481)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [("2.5", "kWh", 2.5), ("2500", "Wh", 2.5), ("1", "MWh", 1000), ("2", "kW", None)],
)
def test_energy_states_are_normalised_to_kwh(value, unit, expected):
    assert parse_energy_kwh(value, unit) == expected
