"""Offline scenarios for a chronological quarter-hour battery planner."""

import asyncio
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.nexus_controller import actuator as actuator_module
from custom_components.nexus_controller import coordinator as coordinator_module
from custom_components.nexus_controller.charging import inverter_efficiency
from custom_components.nexus_controller.const import (
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_ELECTRICITY_PRICE,
    CONF_GRID_POWER_L1,
    CONF_GRID_POWER_L2,
    CONF_GRID_POWER_L3,
    CONF_SOLAR_HOURLY_TODAY,
    CONF_SOLAR_HOURLY_TOMORROW,
    SET_CONTROL_ENABLED,
    SET_DIRECTION_DELAY,
    SET_DISCHARGE_OFFSET,
    SET_EXPORT_INCLUDES_TAX,
    SET_MIN_CONTROL_POWER,
    SET_MIN_DISCHARGE_SOC,
    SET_MIN_EARNINGS,
    SET_PLANNER_OVERRIDE,
    SET_TAX,
    SETTING_DEFAULTS,
)
from custom_components.nexus_controller.coordinator import NexusCoordinator
from custom_components.nexus_controller.dispatch import DispatchPlan
from custom_components.nexus_controller.quarter_planner import (
    current_plan_dispatch,
    forecast_quarters,
    forecast_quarters_with_current,
    plan_quarters,
)
from custom_components.nexus_controller.switch import NexusPlannerSwitch
from tests.test_actuator import _actuator, _calls


def test_planned_dispatch_fails_idle_for_expired_or_missing_measurements():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    slot = {
        "start": now.isoformat(),
        "duration_hours": 0.25,
        "power_w": 5000.0,
        "charge_source": "none",
        "discharge_mode": "export",
    }
    inputs = {
        "now": now,
        "soc_pct": 80.0,
        "min_soc_pct": 10.0,
        "export_reserve_soc_pct": 30.0,
        "max_charge_w": 4000.0,
        "max_discharge_w": 3000.0,
        "grid_import_w": 500.0,
        "battery_power_w": 0.0,
        "measurement_ages_s": (0, 0, 0, 0, 0),
        "max_measurement_age_s": 30.0,
    }

    assert current_plan_dispatch(slot, **inputs).power_w == 3000.0
    assert (
        current_plan_dispatch(slot, **{**inputs, "now": now + timedelta(minutes=16)}).mode == "idle"
    )
    assert current_plan_dispatch(slot, **{**inputs, "grid_import_w": None}).mode == "idle"
    assert current_plan_dispatch(slot, **{**inputs, "soc_pct": 30.0}).mode == "idle"


def test_solar_plan_requires_actual_solar_surplus():
    now = datetime(2026, 11, 9, 12, tzinfo=UTC)
    slot = {
        "start": now.isoformat(),
        "duration_hours": 0.25,
        "power_w": -1000.0,
        "charge_source": "solar",
    }
    inputs = {
        "now": now,
        "soc_pct": 50.0,
        "min_soc_pct": 10.0,
        "export_reserve_soc_pct": 30.0,
        "max_charge_w": 5000.0,
        "max_discharge_w": 5000.0,
        "grid_import_w": 500.0,
        "battery_power_w": 0.0,
        "pv_power_w": 1000.0,
        "measurement_ages_s": (0, 0, 0, 0, 0),
        "max_measurement_age_s": 30.0,
    }

    assert current_plan_dispatch(slot, **inputs).mode == "idle"
    result = current_plan_dispatch(slot, **{**inputs, "grid_import_w": -500.0})
    assert (result.mode, result.reason) == ("charge", "pv_surplus_reduce_export")
    assert result.power_w == 500.0
    assert (
        current_plan_dispatch(slot, **{**inputs, "grid_import_w": -500.0, "pv_power_w": None}).mode
        == "idle"
    )
    capped = current_plan_dispatch(slot, **{**inputs, "grid_import_w": -500.0, "pv_power_w": 200.0})
    assert capped.power_w == 200.0


def test_unknown_discharge_mode_is_not_assumed_to_be_export():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    slot = {"start": now.isoformat(), "duration_hours": 0.25, "power_w": 5000.0}

    result = current_plan_dispatch(
        slot,
        now=now,
        soc_pct=80.0,
        min_soc_pct=10.0,
        export_reserve_soc_pct=30.0,
        max_charge_w=10_000.0,
        max_discharge_w=10_000.0,
        grid_import_w=500.0,
        battery_power_w=0.0,
        measurement_ages_s=(0, 0, 0, 0, 0),
        max_measurement_age_s=30.0,
    )

    assert result.mode == "idle"


def test_planned_export_cannot_exceed_inverter_rating():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    slot = {
        "start": now.isoformat(),
        "duration_hours": 0.25,
        "power_w": 20_000.0,
        "discharge_mode": "export",
    }

    result = current_plan_dispatch(
        slot,
        now=now,
        soc_pct=80.0,
        min_soc_pct=10.0,
        export_reserve_soc_pct=30.0,
        max_charge_w=20_000.0,
        max_discharge_w=20_000.0,
        grid_import_w=500.0,
        battery_power_w=0.0,
        measurement_ages_s=(0, 0, 0, 0, 0),
        max_measurement_age_s=30.0,
    )

    assert result.power_w == 10_000.0


def test_planned_reversal_uses_actuator_idle_delay(monkeypatch):
    actuator, hass = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(actuator_module.time, "monotonic", lambda: clock[0])
    start = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    common = {
        "min_soc_pct": 10.0,
        "export_reserve_soc_pct": 30.0,
        "max_charge_w": 10_000.0,
        "max_discharge_w": 10_000.0,
        "grid_import_w": 0.0,
        "battery_power_w": 0.0,
        "measurement_ages_s": (0, 0, 0, 0, 0),
        "max_measurement_age_s": 30.0,
    }
    charge = {
        "start": start.isoformat(),
        "duration_hours": 0.25,
        "power_w": -5000.0,
        "charge_source": "grid",
    }
    charge_action = current_plan_dispatch(charge, now=start, soc_pct=50.0, **common)
    assert (
        asyncio.run(
            actuator.async_apply(
                charge_action.mode, charge_action.power_w, min_interval_s=10, direction_delay_s=60
            )
        )
        == "mode_started"
    )

    next_start = start + timedelta(minutes=15)
    clock[0] += 900
    sell = {
        "start": next_start.isoformat(),
        "duration_hours": 0.25,
        "power_w": 5000.0,
        "discharge_mode": "export",
    }
    sell_action = current_plan_dispatch(sell, now=next_start, soc_pct=70.0, **common)
    assert (
        asyncio.run(
            actuator.async_apply(
                sell_action.mode, sell_action.power_w, min_interval_s=10, direction_delay_s=60
            )
        )
        == "idle_before_reverse"
    )
    assert _calls(hass)[-2:] == [
        ("switch", "turn_off", {"entity_id": "switch.c"}),
        ("switch", "turn_off", {"entity_id": "switch.d"}),
    ]
    clock[0] += 59
    assert (
        asyncio.run(
            actuator.async_apply(
                sell_action.mode, sell_action.power_w, min_interval_s=10, direction_delay_s=60
            )
        )
        == "waiting_direction_delay"
    )
    clock[0] += 1
    assert (
        asyncio.run(
            actuator.async_apply(
                sell_action.mode, sell_action.power_w, min_interval_s=10, direction_delay_s=60
            )
        )
        == "mode_started"
    )


def test_stale_or_missing_measurement_reports_fail_idle():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    slot = {
        "start": now.isoformat(),
        "duration_hours": 0.25,
        "power_w": 5000.0,
        "discharge_mode": "export",
    }
    inputs = {
        "now": now,
        "soc_pct": 80.0,
        "min_soc_pct": 10.0,
        "export_reserve_soc_pct": 30.0,
        "max_charge_w": 10_000.0,
        "max_discharge_w": 10_000.0,
        "grid_import_w": 500.0,
        "battery_power_w": 0.0,
        "measurement_ages_s": (1.0, 2.0, 3.0, 4.0, 5.0),
        "max_measurement_age_s": 30.0,
    }

    assert current_plan_dispatch(slot, **inputs).mode == "discharge"
    assert current_plan_dispatch(slot, **{**inputs, "measurement_ages_s": None}).mode == "idle"
    assert (
        current_plan_dispatch(slot, **{**inputs, "measurement_ages_s": (1, 2, 31, 4, 5)}).mode
        == "idle"
    )
    assert (
        current_plan_dispatch(slot, **{**inputs, "measurement_ages_s": (1, 2, 3, 4)}).mode == "idle"
    )


@pytest.mark.parametrize(
    "override",
    [
        {"slot": None},
        {"slot": {"start": "not-a-date", "duration_hours": 0.25, "power_w": -1000}},
        {"slot": {"start": "2026-11-09T12:15:00+00:00", "duration_hours": 0.25, "power_w": -1000}},
        {"slot": {"start": "2026-11-09T12:00:00+00:00", "duration_hours": 0, "power_w": -1000}},
        {"battery_power_w": None},
        {"max_charge_w": None},
        {"max_discharge_w": float("nan")},
        {"soc_pct": 100.0},
        {"soc_pct": -1.0},
    ],
)
def test_planned_grid_charging_fails_idle_for_invalid_inputs(override):
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    arguments = {
        "slot": {
            "start": now.isoformat(),
            "duration_hours": 0.25,
            "power_w": -10_000.0,
            "charge_source": "grid",
        },
        "now": now,
        "soc_pct": 50.0,
        "min_soc_pct": 10.0,
        "export_reserve_soc_pct": 30.0,
        "max_charge_w": 4000.0,
        "max_discharge_w": 3000.0,
        "grid_import_w": 500.0,
        "battery_power_w": 0.0,
        "measurement_ages_s": (0, 0, 0, 0, 0),
        "max_measurement_age_s": 30.0,
    }
    arguments.update(override)

    assert current_plan_dispatch(**arguments).mode == "idle"


def test_planned_grid_charging_respects_live_phase_cap():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    slot = {
        "start": now.isoformat(),
        "duration_hours": 0.25,
        "power_w": -10_000.0,
        "charge_source": "grid",
    }

    result = current_plan_dispatch(
        slot,
        now=now,
        soc_pct=50.0,
        min_soc_pct=10.0,
        export_reserve_soc_pct=30.0,
        max_charge_w=4000.0,
        max_discharge_w=3000.0,
        grid_import_w=500.0,
        battery_power_w=0.0,
        measurement_ages_s=(0, 0, 0, 0, 0),
        max_measurement_age_s=30.0,
    )

    assert (result.mode, result.power_w) == ("charge", 4000.0)


def test_nom_discharge_uses_minimum_soc_instead_of_export_reserve():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    slot = {
        "start": now.isoformat(),
        "duration_hours": 0.25,
        "power_w": 1000.0,
        "charge_source": "none",
        "discharge_mode": "nom",
    }
    inputs = {
        "now": now,
        "soc_pct": 20.0,
        "min_soc_pct": 10.0,
        "export_reserve_soc_pct": 30.0,
        "max_charge_w": 4000.0,
        "max_discharge_w": 3000.0,
        "grid_import_w": 500.0,
        "battery_power_w": 0.0,
        "measurement_ages_s": (0, 0, 0, 0, 0),
        "max_measurement_age_s": 30.0,
    }

    result = current_plan_dispatch(slot, **inputs)

    assert (result.mode, result.power_w, result.reason) == (
        "discharge",
        500.0,
        "high_price_no_solar_reduce_import",
    )
    assert current_plan_dispatch(slot, **{**inputs, "soc_pct": 10.0}).mode == "idle"
    assert current_plan_dispatch(slot, **{**inputs, "grid_import_w": -1.0}).mode == "idle"


def test_two_profitable_cycles_beat_one_later_peak():
    prices = [0.30] * 8 + [0.60] * 8 + [0.40] * 8 + [0.80] * 8
    result = plan_quarters(
        prices,
        capacity_kwh=20.0,
        initial_soc_pct=0.0,
        min_soc_pct=0.0,
        max_power_w=10_000.0,
        efficiency=1.0,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
    )

    assert result.profit_eur == pytest.approx(14.0)
    assert result.soc_pct[-1] == pytest.approx(0.0)
    assert sum(power > 0 for power in result.power_w) == 16
    assert sum(power < 0 for power in result.power_w) == 16


def test_power_dependent_efficiency_drives_planned_soc():
    result = plan_quarters(
        [0.10, 0.90],
        capacity_kwh=2.5,
        initial_soc_pct=0.0,
        min_soc_pct=0.0,
        max_power_w=500.0,
        efficiency=0.96,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
        use_power_efficiency=True,
    )

    charged_kwh = -result.power_w[0] / 4000 * inverter_efficiency(result.power_w[0])
    discharged_kwh = result.power_w[1] / 4000 / inverter_efficiency(result.power_w[1])
    assert result.power_w[0] == pytest.approx(-500.0, abs=0.01)
    assert result.soc_pct[0] == pytest.approx(charged_kwh / 2.5 * 100)
    assert result.soc_pct[-1] == pytest.approx(0.0, abs=1e-6)
    assert discharged_kwh == pytest.approx(charged_kwh, abs=1e-7)
    actual_profit = result.power_w[1] / 4000 * 0.90 - (-result.power_w[0] / 4000) * 0.10
    assert result.profit_eur == pytest.approx(actual_profit)


def test_legacy_efficiency_mode_uses_the_same_fixed_average():
    arguments = {
        "capacity_kwh": 2.5,
        "initial_soc_pct": 0.0,
        "min_soc_pct": 0.0,
        "max_power_w": 500.0,
        "efficiency": 0.97,
        "min_profit_eur_per_kwh": 0.15,
        "export_tax_eur_per_kwh": 0.0,
    }
    fixed = plan_quarters([0.25, 0.45], **arguments)
    curve = plan_quarters([0.25, 0.45], use_power_efficiency=True, **arguments)

    assert fixed.profit_eur > 0
    assert curve.profit_eur == pytest.approx(fixed.profit_eur)
    assert curve.power_w == pytest.approx(fixed.power_w)


def test_power_curve_values_solar_energy_at_its_own_efficiency():
    plan = plan_quarters(
        [0.50, 0.90],
        capacity_kwh=2.5,
        initial_soc_pct=0.0,
        min_soc_pct=0.0,
        max_power_w=10_000.0,
        efficiency=0.96,
        min_profit_eur_per_kwh=0.28,
        export_tax_eur_per_kwh=0.11,
        solar_kwh=[0.25, 0.0],
        use_power_efficiency=True,
    )

    assert plan.charge_source[0] == "solar"
    charged_kwh = -plan.power_w[0] / 4000 * inverter_efficiency(plan.power_w[0], pv_share=1)
    assert plan.soc_pct[0] == pytest.approx(charged_kwh / 2.5 * 100)
    assert plan.soc_pct[-1] == pytest.approx(0.0, abs=1e-6)
    actual_profit = plan.power_w[1] / 4000 * (0.90 - 0.11) - (-plan.power_w[0] / 4000) * (
        0.50 - 0.11
    )
    assert plan.profit_eur == pytest.approx(actual_profit)


@pytest.mark.parametrize("use_power_efficiency", [False, True])
def test_randomized_plans_obey_energy_soc_and_power_limits(use_power_efficiency):
    randomizer = random.Random(1907)
    for _ in range(100):
        prices = [randomizer.uniform(-0.1, 0.9) for _ in range(12)]
        solar = [randomizer.uniform(0, 1) for _ in prices]
        initial = randomizer.uniform(10, 100)
        efficiency = randomizer.uniform(0.85, 1.0)
        durations = [randomizer.uniform(0.02, 0.25), *([0.25] * 11)]
        result = plan_quarters(
            prices,
            capacity_kwh=20.8,
            initial_soc_pct=initial,
            min_soc_pct=10.0,
            max_power_w=10_000.0,
            efficiency=efficiency,
            min_profit_eur_per_kwh=0.15,
            export_tax_eur_per_kwh=0.11,
            initial_value_eur_per_kwh=0.25,
            house_load_w=500.0,
            solar_kwh=solar,
            durations_hours=durations,
            use_power_efficiency=use_power_efficiency,
        )

        stored = 20.8 * initial / 100
        for power, soc, duration, source, pv in zip(
            result.power_w, result.soc_pct, durations, result.charge_source, solar, strict=True
        ):
            assert abs(power) <= 10_000 + 1e-7
            assert 10 - 1e-7 <= soc <= 100 + 1e-7
            effective = efficiency
            if use_power_efficiency and power < 0:
                charge_energy = -power * duration / 1000
                solar_share = min(1.0, max(0, pv - 500 * duration / 1000) / charge_energy)
                effective = inverter_efficiency(power, pv_share=solar_share)
            elif use_power_efficiency and power > 0:
                effective = inverter_efficiency(power)
            stored += (-power * effective if power < 0 else -power / effective) * duration / 1000
            assert soc == pytest.approx(stored / 20.8 * 100)
            if source == "solar":
                assert -power * duration / 1000 <= max(0, pv - 500 * duration / 1000) + 1e-7


def test_each_cycle_must_meet_its_own_margin():
    result = plan_quarters(
        [0.40, 0.60, 0.55, 0.65],
        capacity_kwh=2.5,
        initial_soc_pct=0.0,
        min_soc_pct=0.0,
        max_power_w=10_000.0,
        efficiency=1.0,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
    )

    assert result.power_w == pytest.approx((-10_000, 0, 0, 10_000))
    assert result.profit_eur == pytest.approx(0.625)


def test_tax_losses_and_reserve_bound_the_plan():
    result = plan_quarters(
        [0.20, 0.60],
        capacity_kwh=10.0,
        initial_soc_pct=80.0,
        min_soc_pct=10.0,
        max_power_w=10_000.0,
        efficiency=0.9,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.11,
    )

    assert all(10 <= value <= 100 for value in result.soc_pct)
    assert result.soc_pct[-1] == pytest.approx(80.0)
    assert result.power_w[0] == pytest.approx(-2 / 0.9 * 4000)
    assert result.power_w[1] == pytest.approx(2 * 0.9 * 4000)
    assert result.profit_eur == pytest.approx(1.8 * (0.60 - 0.11 - 0.20 / 0.9**2))


def test_cannot_discharge_at_a_peak_before_charging():
    result = plan_quarters(
        [0.90, 0.10],
        capacity_kwh=5.0,
        initial_soc_pct=50.0,
        min_soc_pct=10.0,
        max_power_w=10_000.0,
        efficiency=0.95,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
    )

    assert result.power_w == (0.0, 0.0)
    assert result.profit_eur == 0.0


def test_existing_full_battery_can_discharge_at_a_profitable_peak():
    result = plan_quarters(
        [0.20, 0.80, 0.20, 0.70],
        capacity_kwh=2.5,
        initial_soc_pct=100.0,
        min_soc_pct=0.0,
        max_power_w=10_000.0,
        efficiency=1.0,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
        initial_value_eur_per_kwh=0.30,
    )

    assert result.power_w == pytest.approx((0, 10_000, -10_000, 10_000))
    assert result.soc_pct == pytest.approx((100, 0, 100, 0))
    assert result.profit_eur == pytest.approx(2.5)


def test_existing_inventory_respects_minimum_soc_and_profit():
    settings = {
        "capacity_kwh": 10.0,
        "initial_soc_pct": 50.0,
        "min_soc_pct": 10.0,
        "max_power_w": 10_000.0,
        "efficiency": 1.0,
        "min_profit_eur_per_kwh": 0.15,
        "export_tax_eur_per_kwh": 0.0,
        "initial_value_eur_per_kwh": 0.30,
    }

    insufficient = plan_quarters([0.40, 0.40], **settings)
    profitable = plan_quarters([0.60, 0.60], **settings)

    assert insufficient.power_w == (0.0, 0.0)
    assert profitable.soc_pct[-1] == pytest.approx(10.0)
    assert all(10 <= soc <= 100 for soc in profitable.soc_pct)
    assert profitable.profit_eur == pytest.approx(1.2)


def test_initial_value_is_not_guessed_when_missing():
    result = plan_quarters(
        [0.80, 0.80],
        capacity_kwh=2.5,
        initial_soc_pct=100.0,
        min_soc_pct=0.0,
        max_power_w=10_000.0,
        efficiency=1.0,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
    )

    assert result.power_w == (0.0, 0.0)


def test_short_peak_can_discharge_part_of_existing_inventory():
    result = plan_quarters(
        [0.20, 0.80, 0.20],
        capacity_kwh=20.0,
        initial_soc_pct=100.0,
        min_soc_pct=10.0,
        max_power_w=10_000.0,
        efficiency=1.0,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
        initial_value_eur_per_kwh=0.30,
    )

    assert result.power_w == pytest.approx((0, 10_000, 0))
    assert result.soc_pct[-1] == pytest.approx(87.5)
    assert result.profit_eur == pytest.approx(1.25)


def test_initial_partial_quarter_can_discharge_existing_inventory():
    result = plan_quarters(
        [0.80, -0.50],
        capacity_kwh=20.8,
        initial_soc_pct=85.0,
        min_soc_pct=10.0,
        max_power_w=10_000.0,
        efficiency=0.97,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
        initial_value_eur_per_kwh=0.10,
        durations_hours=[14 / 60, 0.25],
    )

    assert result.power_w == pytest.approx((10_000.0, 0.0))


def test_partial_initial_discharge_respects_profit_and_reserve():
    settings = {
        "capacity_kwh": 20.0,
        "initial_soc_pct": 11.0,
        "min_soc_pct": 10.0,
        "max_power_w": 10_000.0,
        "efficiency": 1.0,
        "min_profit_eur_per_kwh": 0.15,
        "export_tax_eur_per_kwh": 0.0,
        "initial_value_eur_per_kwh": 0.30,
    }

    blocked = plan_quarters([0.40], **settings)
    profitable = plan_quarters([0.80], **settings)

    assert blocked.power_w == (0.0,)
    assert profitable.power_w == pytest.approx((800.0,))
    assert profitable.soc_pct[-1] == pytest.approx(10.0)
    assert profitable.profit_eur == pytest.approx(0.10)


def test_partial_sale_then_profitable_recharge_and_sale():
    result = plan_quarters(
        [0.80, 0.20, 0.70],
        capacity_kwh=5.0,
        initial_soc_pct=100.0,
        min_soc_pct=0.0,
        max_power_w=10_000.0,
        efficiency=1.0,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
        initial_value_eur_per_kwh=0.30,
    )

    assert result.power_w == pytest.approx((10_000, -10_000, 10_000))
    assert result.profit_eur == pytest.approx(2.5)


def test_initial_inventory_can_be_sold_across_separate_peaks():
    result = plan_quarters(
        [0.80, 0.20, 0.70, 0.90],
        capacity_kwh=5.0,
        initial_soc_pct=100.0,
        min_soc_pct=0.0,
        max_power_w=10_000.0,
        efficiency=1.0,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
        initial_value_eur_per_kwh=0.30,
    )

    assert result.power_w == pytest.approx((10_000, -10_000, 10_000, 10_000))
    assert result.profit_eur == pytest.approx(4.0)


def test_planning_fails_closed_when_search_would_be_excessive():
    with pytest.raises(ValueError, match="work limit"):
        plan_quarters(
            [0.30] * 200,
            capacity_kwh=20.0,
            initial_soc_pct=50.0,
            min_soc_pct=10.0,
            max_power_w=1.0,
            efficiency=0.95,
            min_profit_eur_per_kwh=0.15,
            export_tax_eur_per_kwh=0.0,
            initial_value_eur_per_kwh=0.30,
        )


def test_house_consumption_is_valued_at_import_price():
    settings = {
        "capacity_kwh": 2.5,
        "initial_soc_pct": 0.0,
        "min_soc_pct": 0.0,
        "max_power_w": 10_000.0,
        "efficiency": 1.0,
        "min_profit_eur_per_kwh": 0.15,
        "export_tax_eur_per_kwh": 0.11,
    }
    exported = plan_quarters([0.30, 0.50], **settings)
    consumed = plan_quarters([0.30, 0.50], house_load_w=10_000, **settings)

    assert exported.profit_eur == 0
    assert consumed.profit_eur == pytest.approx(0.50)


def test_plan_reserves_soc_for_export_but_can_use_nom_below_reserve():
    settings = {
        "capacity_kwh": 20.0,
        "initial_soc_pct": 10.0,
        "min_soc_pct": 10.0,
        "export_reserve_soc_pct": 30.0,
        "max_power_w": 10_000.0,
        "efficiency": 1.0,
        "min_profit_eur_per_kwh": 0.15,
        "export_tax_eur_per_kwh": 0.11,
    }
    without_house = plan_quarters([0.20, 0.80], house_load_w=0.0, **settings)
    with_house = plan_quarters([0.20, 0.80], house_load_w=500.0, **settings)

    assert without_house.power_w == (0.0, 0.0)
    assert with_house.power_w[1] == pytest.approx(500.0)
    assert with_house.soc_pct[-1] == pytest.approx(10.0)


def test_solar_reduces_the_household_demand_available_for_nom_below_reserve():
    plan = plan_quarters(
        [0.20, 0.80],
        capacity_kwh=20.0,
        initial_soc_pct=10.0,
        min_soc_pct=10.0,
        export_reserve_soc_pct=30.0,
        max_power_w=10_000.0,
        efficiency=1.0,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.11,
        house_load_w=1000.0,
        solar_kwh=[0.0, 0.125],
    )

    assert plan.power_w[1] == pytest.approx(500.0)
    assert plan.soc_pct[-1] == pytest.approx(10.0)


def test_future_slots_use_their_own_export_tax():
    settings = {
        "capacity_kwh": 2.5,
        "initial_soc_pct": 0.0,
        "min_soc_pct": 0.0,
        "max_power_w": 10_000.0,
        "efficiency": 1.0,
        "min_profit_eur_per_kwh": 0.15,
        "export_tax_eur_per_kwh": 0.0,
    }
    taxed = plan_quarters([0.30, 0.50], export_taxes_eur_per_kwh=[0.0, 0.11], **settings)
    untaxed = plan_quarters([0.30, 0.50], export_taxes_eur_per_kwh=[0.11, 0.0], **settings)

    assert taxed.profit_eur == 0
    assert untaxed.profit_eur == pytest.approx(0.50)


def test_solar_charging_accounts_for_foregone_export_revenue():
    settings = {
        "capacity_kwh": 2.5,
        "initial_soc_pct": 0.0,
        "min_soc_pct": 0.0,
        "max_power_w": 10_000.0,
        "efficiency": 1.0,
        "min_profit_eur_per_kwh": 0.15,
        "export_tax_eur_per_kwh": 0.11,
        "house_load_w": 0.0,
    }
    plan = plan_quarters([0.20, 0.60], solar_kwh=[2.5, 0.0], **settings)

    assert plan.power_w == pytest.approx((-10_000, 10_000))
    assert plan.soc_pct == pytest.approx((100, 0))
    assert plan.profit_eur == pytest.approx(1.0)


def test_partially_elapsed_quarter_limits_charging_energy():
    plan = plan_quarters(
        [0.20, 0.60],
        capacity_kwh=2.5,
        initial_soc_pct=0.0,
        min_soc_pct=0.0,
        max_power_w=10_000.0,
        efficiency=1.0,
        min_profit_eur_per_kwh=0.15,
        export_tax_eur_per_kwh=0.0,
        durations_hours=[0.125, 0.25],
    )

    assert plan.power_w == pytest.approx((-10_000, 5_000))
    assert plan.soc_pct == pytest.approx((50, 0))
    assert plan.profit_eur == pytest.approx(0.5)


def test_solar_meets_house_demand_before_it_can_charge_the_battery():
    settings = {
        "capacity_kwh": 2.5,
        "initial_soc_pct": 0.0,
        "min_soc_pct": 0.0,
        "max_power_w": 10_000.0,
        "efficiency": 1.0,
        "min_profit_eur_per_kwh": 0.0,
        "export_tax_eur_per_kwh": 0.11,
        "house_load_w": 10_000.0,
    }
    plan = plan_quarters([0.20, 0.60], solar_kwh=[2.5, 0.0], **settings)

    assert plan.power_w == pytest.approx((-10_000, 10_000))
    assert plan.profit_eur == pytest.approx(1.0)


def test_invalid_solar_series_is_rejected():
    settings = {
        "capacity_kwh": 2.5,
        "initial_soc_pct": 0.0,
        "min_soc_pct": 0.0,
        "max_power_w": 10_000.0,
        "efficiency": 1.0,
        "min_profit_eur_per_kwh": 0.15,
        "export_tax_eur_per_kwh": 0.11,
    }
    with pytest.raises(ValueError):
        plan_quarters([0.20, 0.60], solar_kwh=[0.0], **settings)
    with pytest.raises(ValueError):
        plan_quarters([0.20, 0.60], solar_kwh=[0.0, -0.1], **settings)


def test_solar_only_cycle_does_not_buy_unprofitable_grid_energy():
    plan = plan_quarters(
        [0.50, 0.75],
        capacity_kwh=2.5,
        initial_soc_pct=0.0,
        min_soc_pct=0.0,
        max_power_w=10_000.0,
        efficiency=1.0,
        min_profit_eur_per_kwh=0.20,
        export_tax_eur_per_kwh=0.11,
        solar_kwh=[0.25, 0.0],
    )

    assert plan.power_w == pytest.approx((-1000, 1000))
    assert plan.charge_source[0] == "solar"
    assert plan.profit_eur == pytest.approx(0.0625)


def test_forecast_requires_contiguous_full_quarters():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    start = now.replace(minute=15)
    valid = [(start, 0.30), (start + timedelta(minutes=15), 0.60)]

    assert forecast_quarters(valid, now) == valid
    assert forecast_quarters([valid[0], (start + timedelta(minutes=30), 0.60)], now) == []
    distant = [(start + timedelta(hours=1), 0.30), (start + timedelta(hours=1, minutes=15), 0.60)]
    assert forecast_quarters(distant, now) == []


def test_current_interval_uses_remaining_time_and_rejects_gaps():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    start = now.replace(minute=0)
    forecast = [(start, 0.20), (start + timedelta(minutes=15), 0.60)]

    result = forecast_quarters_with_current(forecast, now)

    assert result[0][0] == start
    assert result[0][2] == pytest.approx(10 / 60)
    assert result[1][2] == 0.25
    assert (
        forecast_quarters_with_current([forecast[0], (start + timedelta(minutes=30), 0.60)], now)
        == []
    )


def test_diagnostic_plan_is_read_only_and_exposes_quarters():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    start = now.replace(minute=15)

    async def executor(function):
        return function()

    coordinator = SimpleNamespace(
        price_forecast=[(start, 0.20), (start + timedelta(minutes=15), 0.70)],
        price_forecast_taxes={},
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: True,
        },
        tracker=SimpleNamespace(state=SimpleNamespace(avg_daytime_load_w=500.0, stored_value=0.30)),
        hass=SimpleNamespace(async_add_executor_job=executor),
        _experimental_solar_quarters=lambda now: {},
    )
    data = {CONF_BATTERY_SOC: 50.0, "tax_used": 0.11}

    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (5000, 5000))
    )

    assert data["experimental_quarter_plan"] == "experimental"
    assert coordinator.experimental_quarter_plan["export_tax_eur_kwh"] == 0
    assert len(coordinator.experimental_quarter_plan["slots"]) == 2
    assert coordinator.experimental_quarter_plan["slots"][0]["power_w"] < 0
    assert coordinator.experimental_quarter_plan["slots"][1]["power_w"] > 0
    assert coordinator.experimental_quarter_plan["slots"][1]["discharge_mode"] == "export"

    coordinator.tracker.state.avg_daytime_load_w = 10_000.0
    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (5000, 5000))
    )
    assert coordinator.experimental_quarter_plan["slots"][1]["discharge_mode"] == "nom"


@pytest.mark.parametrize(
    ("power", "source", "mode", "projected_soc"),
    [
        (6000.0, "none", "export", 85.0 - 6000 * 10 / 60 / 1000 / 0.97 / 20.8 * 100),
        (-6000.0, "grid", "none", 85.0 + 6000 * 10 / 60 / 1000 * 0.97 / 20.8 * 100),
        (0.0, "none", "none", 85.0),
    ],
)
def test_replanning_keeps_active_quarter_and_projects_its_soc(power, source, mode, projected_soc):
    start = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    now = start + timedelta(minutes=5)

    async def executor(function):
        return function()

    coordinator = SimpleNamespace(
        price_forecast=[(start, 0.80), (start + timedelta(minutes=15), -0.50)],
        price_forecast_taxes={},
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: True,
        },
        tracker=SimpleNamespace(state=SimpleNamespace(avg_daytime_load_w=0.0, stored_value=0.10)),
        hass=SimpleNamespace(async_add_executor_job=executor),
        _experimental_solar_quarters=lambda moment: {},
        experimental_quarter_plan={
            "minimum_earnings_eur_kwh": 0.15,
            "slots": [{
                "start": start.isoformat(),
                "forecast_start": start.isoformat(),
                "duration_hours": 0.25,
                "price_eur_kwh": 0.80,
                "export_tax_eur_kwh": 0.0,
                "power_w": power,
                "charge_source": source,
                "discharge_mode": mode,
            }],
        },
    )
    data = {CONF_BATTERY_SOC: 85.0, "tax_used": 0.0}

    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (10000, 10000))
    )

    active, future = coordinator.experimental_quarter_plan["slots"]
    assert active["power_w"] == power
    assert active["charge_source"] == source
    assert active["discharge_mode"] == mode
    assert active["soc_pct"] == pytest.approx(projected_soc, abs=0.01)
    assert future["soc_pct"] == pytest.approx(projected_soc, abs=0.01)

    coordinator.settings[SET_MIN_EARNINGS] = 0.20
    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (10000, 10000))
    )
    assert data["experimental_quarter_plan"] == "unavailable"
    coordinator.experimental_quarter_plan = {}
    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (10000, 10000))
    )
    assert data["experimental_quarter_plan"] == "unavailable"

    coordinator.settings[SET_MIN_EARNINGS] = 0.15
    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (10000, 10000))
    )
    assert coordinator.experimental_quarter_plan["slots"][0]["power_w"] == power

    coordinator.price_forecast[0] = (start, 0.90)
    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (10000, 10000))
    )
    assert data["experimental_quarter_plan"] == "unavailable"
    coordinator.experimental_quarter_plan = {}
    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (10000, 10000))
    )
    assert data["experimental_quarter_plan"] == "unavailable"

    coordinator.price_forecast[0] = (start, 0.80)
    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (10000, 10000))
    )
    next_power = coordinator.experimental_quarter_plan["slots"][1]["power_w"]
    coordinator.price_forecast.append((start + timedelta(minutes=30), 0.90))
    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(
            coordinator, data, start + timedelta(minutes=15), (10000, 10000)
        )
    )
    assert coordinator.experimental_quarter_plan["slots"][0]["power_w"] == next_power


@pytest.mark.parametrize(
    ("soc", "limit", "projected_soc"),
    [(85.0, 2000, 85.0 - 2000 * 10 / 60 / 1000 / 0.97 / 20.8 * 100), (31.0, 10000, 30.0)],
)
def test_retained_export_projection_respects_power_cap_and_reserve(soc, limit, projected_soc):
    start = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    now = start + timedelta(minutes=5)

    async def executor(function):
        return function()

    coordinator = SimpleNamespace(
        price_forecast=[(start, 0.80), (start + timedelta(minutes=15), -0.50)],
        price_forecast_taxes={},
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: False,
            SET_TAX: 0.11,
        },
        tracker=SimpleNamespace(state=SimpleNamespace(avg_daytime_load_w=0.0, stored_value=0.10)),
        hass=SimpleNamespace(async_add_executor_job=executor),
        _experimental_solar_quarters=lambda moment: {},
        experimental_quarter_plan={
            "minimum_earnings_eur_kwh": 0.15,
            "slots": [{
                "forecast_start": start.isoformat(),
                "price_eur_kwh": 0.80,
                "export_tax_eur_kwh": 0.11,
                "power_w": 6000.0,
                "charge_source": "none",
                "discharge_mode": "export",
            }],
        },
    )
    data = {CONF_BATTERY_SOC: soc, "tax_used": 0.11}

    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (limit, limit))
    )

    active, future = coordinator.experimental_quarter_plan["slots"]
    assert active["power_w"] == 6000.0
    assert active["soc_pct"] == pytest.approx(projected_soc, abs=0.01)
    assert future["soc_pct"] == pytest.approx(projected_soc, abs=0.01)


def test_planner_measurement_age_uses_reports_not_only_state_changes():
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    keys = (
        CONF_GRID_POWER_L1,
        CONF_GRID_POWER_L2,
        CONF_GRID_POWER_L3,
        CONF_BATTERY_SOC,
        CONF_BATTERY_POWER,
    )
    states = {
        key: SimpleNamespace(
            state="10",
            last_reported=now - timedelta(seconds=5),
            last_updated=now - timedelta(hours=2),
        )
        for key in keys
    }
    coordinator = SimpleNamespace(
        configured_entity_id=lambda key: key,
        hass=SimpleNamespace(states=states),
    )

    assert NexusCoordinator._planner_measurement_ages(coordinator, now) == (5, 5, 5, 5, 5)
    states[CONF_BATTERY_POWER].state = "unavailable"
    assert NexusCoordinator._planner_measurement_ages(coordinator, now) is None


def test_coordinator_rejects_stale_plan_and_changed_current_price():
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    slot = {
        "start": now.isoformat(),
        "forecast_start": now.isoformat(),
        "duration_hours": 0.25,
        "price_eur_kwh": 0.50,
        "export_tax_eur_kwh": 0.11,
        "power_w": 5000.0,
        "discharge_mode": "export",
    }
    coordinator = SimpleNamespace(
        experimental_quarter_plan={
            "generated_at": now.isoformat(),
            "minimum_earnings_eur_kwh": 0.15,
            "slots": [slot],
        },
        _quarter_status="experimental",
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: False,
            SET_TAX: 0.11,
        },
        sample_estimator=SimpleNamespace(interval_s=10),
        price_forecast=[(now, 0.50)],
        price_forecast_taxes={},
        _planner_measurement_ages=lambda moment: (1, 1, 1, 1, 1),
    )
    data = {CONF_BATTERY_SOC: 80.0, CONF_ELECTRICITY_PRICE: 0.50}

    def action(moment, values=data):
        return NexusCoordinator._planner_action(
            coordinator, values, moment, (100.0, 100.0, 100.0), 0.0, (3000.0, 3000.0)
        )

    assert action(now).power_w == 3000.0
    mismatch = action(now, {**data, CONF_ELECTRICITY_PRICE: 0.60})
    assert (mismatch.mode, mismatch.reason) == ("idle", "planner_price_mismatch")
    assert action(now + timedelta(seconds=300)).mode == "discharge"
    assert action(now + timedelta(seconds=361)).mode == "idle"
    coordinator._planner_measurement_ages = lambda moment: (1, 1, 100, 1, 1)
    assert action(now).mode == "idle"


def test_coordinator_export_reserve_follows_tax_policy():
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    slot = {
        "start": now.isoformat(),
        "forecast_start": now.isoformat(),
        "duration_hours": 0.25,
        "price_eur_kwh": 0.50,
        "export_tax_eur_kwh": 0.0,
        "power_w": 5000.0,
        "discharge_mode": "export",
    }
    coordinator = SimpleNamespace(
        experimental_quarter_plan={
            "generated_at": now.isoformat(),
            "minimum_earnings_eur_kwh": 0.15,
            "slots": [slot],
        },
        _quarter_status="experimental",
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: True,
            SET_TAX: 0.11,
        },
        sample_estimator=SimpleNamespace(interval_s=10),
        price_forecast=[(now, 0.50)],
        price_forecast_taxes={},
        _planner_measurement_ages=lambda moment: (1, 1, 1, 1, 1),
    )
    data = {CONF_BATTERY_SOC: 20.0, CONF_ELECTRICITY_PRICE: 0.50}

    def action():
        return NexusCoordinator._planner_action(
            coordinator, data, now, (100.0, 100.0, 100.0), 0.0, (3000.0, 3000.0)
        )

    assert action().mode == "discharge"
    data[CONF_BATTERY_SOC] = 10.0
    assert action().mode == "idle"
    data[CONF_BATTERY_SOC] = 20.0
    coordinator.settings[SET_EXPORT_INCLUDES_TAX] = False
    slot["export_tax_eur_kwh"] = 0.11
    assert action().mode == "idle"


def test_existing_strategy_export_reserve_follows_tax_policy():
    coordinator = SimpleNamespace(
        settings={**SETTING_DEFAULTS, SET_MIN_DISCHARGE_SOC: 10, SET_EXPORT_INCLUDES_TAX: True},
        _discharge_threshold=lambda: 0.25,
    )

    assert NexusCoordinator._shadow_settings(coordinator, 0.11).export.reserve_soc_pct == 10
    coordinator.settings[SET_EXPORT_INCLUDES_TAX] = False
    assert NexusCoordinator._shadow_settings(coordinator, 0.11).export.reserve_soc_pct == 30
    coordinator.settings[SET_MIN_DISCHARGE_SOC] = 40
    assert NexusCoordinator._shadow_settings(coordinator, 0.11).export.reserve_soc_pct == 40


def test_diagnostic_planner_uses_conditional_export_reserve():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    start = now.replace(minute=15)

    async def executor(function):
        return function()

    coordinator = SimpleNamespace(
        price_forecast=[(start, 0.20), (start + timedelta(minutes=15), 0.70)],
        price_forecast_taxes={},
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: True,
            SET_TAX: 0.11,
        },
        tracker=SimpleNamespace(state=SimpleNamespace(avg_daytime_load_w=500.0, stored_value=0.30)),
        hass=SimpleNamespace(async_add_executor_job=executor),
        _experimental_solar_quarters=lambda moment: {},
    )
    data = {CONF_BATTERY_SOC: 10.0, "tax_used": 0.11}

    def run():
        asyncio.run(
            NexusCoordinator._async_experimental_quarter_plan(
                coordinator, data, now, (10000, 10000)
            )
        )
        return coordinator.experimental_quarter_plan["slots"][1]["power_w"]

    assert run() > 500
    coordinator.settings[SET_EXPORT_INCLUDES_TAX] = False
    assert 0 < run() <= 500


def test_coordinator_rejects_a_future_tariff_as_the_current_slot():
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    future = now + timedelta(minutes=15)
    slot = {
        "start": now.isoformat(),
        "forecast_start": future.isoformat(),
        "duration_hours": 0.25,
        "price_eur_kwh": 0.50,
        "export_tax_eur_kwh": 0.11,
        "power_w": 5000.0,
        "discharge_mode": "export",
    }
    coordinator = SimpleNamespace(
        experimental_quarter_plan={
            "generated_at": now.isoformat(),
            "minimum_earnings_eur_kwh": 0.15,
            "slots": [slot],
        },
        _quarter_status="experimental",
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: False,
            SET_TAX: 0.11,
        },
        sample_estimator=SimpleNamespace(interval_s=10),
        price_forecast=[(future, 0.50)],
        price_forecast_taxes={},
        _planner_measurement_ages=lambda moment: (1, 1, 1, 1, 1),
    )

    result = NexusCoordinator._planner_action(
        coordinator,
        {CONF_BATTERY_SOC: 80.0, CONF_ELECTRICITY_PRICE: 0.50},
        now,
        (100.0, 100.0, 100.0),
        0.0,
        (3000.0, 3000.0),
    )

    assert result.mode == "idle"


def test_coordinator_rejects_plan_when_future_price_changes():
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    future = now + timedelta(minutes=15)
    slots = [
        {
            "start": now.isoformat(),
            "forecast_start": now.isoformat(),
            "duration_hours": 0.25,
            "price_eur_kwh": 0.50,
            "export_tax_eur_kwh": 0.11,
            "power_w": 5000.0,
            "discharge_mode": "export",
        },
        {
            "start": future.isoformat(),
            "forecast_start": future.isoformat(),
            "duration_hours": 0.25,
            "price_eur_kwh": 0.70,
            "export_tax_eur_kwh": 0.11,
            "power_w": 0.0,
        },
    ]
    coordinator = SimpleNamespace(
        experimental_quarter_plan={
            "generated_at": now.isoformat(),
            "minimum_earnings_eur_kwh": 0.15,
            "slots": slots,
        },
        _quarter_status="experimental",
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: False,
            SET_TAX: 0.11,
        },
        sample_estimator=SimpleNamespace(interval_s=10),
        price_forecast=[(now, 0.50), (future, 0.70)],
        price_forecast_taxes={},
        _planner_measurement_ages=lambda moment: (1, 1, 1, 1, 1),
    )
    data = {CONF_BATTERY_SOC: 80.0, CONF_ELECTRICITY_PRICE: 0.50}

    def action():
        return NexusCoordinator._planner_action(
            coordinator, data, now, (100.0, 100.0, 100.0), 0.0, (3000.0, 3000.0)
        )

    assert action().mode == "discharge"
    coordinator.price_forecast = [(now, 0.50), (future, 0.40)]
    assert action().mode == "idle"


def test_coordinator_rejects_changed_tax_or_minimum_earnings():
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    slot = {
        "start": now.isoformat(),
        "forecast_start": now.isoformat(),
        "duration_hours": 0.25,
        "price_eur_kwh": 0.50,
        "export_tax_eur_kwh": 0.11,
        "power_w": 5000.0,
        "discharge_mode": "export",
    }
    coordinator = SimpleNamespace(
        experimental_quarter_plan={
            "generated_at": now.isoformat(),
            "minimum_earnings_eur_kwh": 0.15,
            "slots": [slot],
        },
        _quarter_status="experimental",
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: False,
            SET_TAX: 0.11,
        },
        sample_estimator=SimpleNamespace(interval_s=10),
        price_forecast=[(now, 0.50)],
        price_forecast_taxes={},
        _planner_measurement_ages=lambda moment: (1, 1, 1, 1, 1),
    )
    data = {CONF_BATTERY_SOC: 80.0, CONF_ELECTRICITY_PRICE: 0.50}

    def action():
        return NexusCoordinator._planner_action(
            coordinator, data, now, (100.0, 100.0, 100.0), 0.0, (3000.0, 3000.0)
        )

    assert action().mode == "discharge"
    coordinator.settings[SET_MIN_EARNINGS] = 0.20
    assert action().mode == "idle"
    coordinator.settings[SET_MIN_EARNINGS] = 0.15
    coordinator.settings[SET_EXPORT_INCLUDES_TAX] = True
    assert action().mode == "idle"
    coordinator.settings[SET_EXPORT_INCLUDES_TAX] = False
    coordinator.price_forecast_taxes[now] = 0.12
    assert action().mode == "idle"


def test_override_uses_safe_planner_action_instead_of_existing_strategy():
    actuator = SimpleNamespace(async_apply=AsyncMock(return_value="idle"))
    coordinator = SimpleNamespace(
        settings={
            SET_CONTROL_ENABLED: True,
            SET_DIRECTION_DELAY: 60,
            "planner_override_enabled": True,
        },
        _manual_mode=None,
        _controlled_samples=0,
        _grid_samples=1,
        _control_status="enabled",
        _control_was_on=False,
        _pid_memory=None,
        sample_estimator=SimpleNamespace(interval_s=10),
        _planner_action=lambda *args: DispatchPlan("idle", 0, "planner_unavailable_or_stale"),
        _get_actuator=lambda: actuator,
    )

    asyncio.run(
        NexusCoordinator._async_control(
            coordinator,
            {},
            DispatchPlan("charge", 5000, "net_charge"),
            (100.0, 100.0, 100.0),
            0.0,
            (5000.0, 5000.0),
        )
    )

    assert actuator.async_apply.await_args.args[:2] == ("idle", 0.0)


def test_planner_watchdog_stops_without_a_new_grid_sample():
    actuator = SimpleNamespace(async_apply=AsyncMock(return_value="idle"))
    coordinator = SimpleNamespace(
        settings={SET_CONTROL_ENABLED: True, SET_DIRECTION_DELAY: 60, SET_PLANNER_OVERRIDE: True},
        _manual_mode=None,
        _controlled_samples=1,
        _grid_samples=1,
        _control_status="holding",
        _control_was_on=True,
        _pid_memory=None,
        sample_estimator=SimpleNamespace(interval_s=10),
        _planner_action=lambda *args: DispatchPlan("idle", 0, "planner_unavailable_or_stale"),
        _get_actuator=lambda: actuator,
    )

    asyncio.run(
        NexusCoordinator._async_control(
            coordinator,
            {},
            DispatchPlan("charge", 5000, "net_charge"),
            (100.0, 100.0, 100.0),
            0.0,
            (5000.0, 5000.0),
        )
    )

    assert actuator.async_apply.await_args.args[:2] == ("idle", 0.0)


def test_planner_watchdog_does_not_rewrite_a_valid_command_without_new_sample():
    actuator = SimpleNamespace(async_apply=AsyncMock(return_value="holding"))
    coordinator = SimpleNamespace(
        settings={SET_CONTROL_ENABLED: True, SET_DIRECTION_DELAY: 60, SET_PLANNER_OVERRIDE: True},
        _manual_mode=None,
        _controlled_samples=1,
        _grid_samples=1,
        _control_status="holding",
        _control_was_on=True,
        _pid_memory=None,
        sample_estimator=SimpleNamespace(interval_s=10),
        _planner_action=lambda *args: DispatchPlan("discharge", 500, "planned_export"),
        _get_actuator=lambda: actuator,
    )

    asyncio.run(
        NexusCoordinator._async_control(
            coordinator,
            {},
            DispatchPlan("idle", 0, "no_dispatch_condition"),
            (100.0, 100.0, 100.0),
            0.0,
            (5000.0, 5000.0),
        )
    )

    actuator.async_apply.assert_not_awaited()


def test_offline_override_chain_stops_when_measurements_become_stale(monkeypatch):
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: now)
    actuator, hass = _actuator()
    slot = {
        "start": now.isoformat(),
        "forecast_start": now.isoformat(),
        "duration_hours": 0.25,
        "price_eur_kwh": 0.50,
        "export_tax_eur_kwh": 0.11,
        "power_w": 5000.0,
        "discharge_mode": "export",
    }
    coordinator = SimpleNamespace(
        experimental_quarter_plan={
            "generated_at": now.isoformat(),
            "minimum_earnings_eur_kwh": 0.15,
            "slots": [slot],
        },
        _quarter_status="experimental",
        settings={
            SET_CONTROL_ENABLED: True,
            SET_DIRECTION_DELAY: 60,
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: False,
            SET_TAX: 0.11,
            "planner_override_enabled": True,
        },
        _manual_mode=None,
        _controlled_samples=0,
        _grid_samples=1,
        _control_status="enabled",
        _control_was_on=False,
        _pid_memory=None,
        sample_estimator=SimpleNamespace(interval_s=10),
        price_forecast=[(now, 0.50)],
        price_forecast_taxes={},
        _planner_measurement_ages=lambda moment: (1, 1, 1, 1, 1),
        _get_actuator=lambda: actuator,
    )
    coordinator._planner_action = lambda *args: NexusCoordinator._planner_action(coordinator, *args)
    data = {CONF_BATTERY_SOC: 80.0, CONF_ELECTRICITY_PRICE: 0.50}
    legacy = DispatchPlan("idle", 0, "no_dispatch_condition")

    asyncio.run(
        NexusCoordinator._async_control(
            coordinator, data, legacy, (100.0, 100.0, 100.0), 0.0, (3000.0, 3000.0)
        )
    )
    assert _calls(hass)[-2:] == [
        ("number", "set_value", {"entity_id": "number.dp", "value": 3000}),
        ("switch", "turn_on", {"entity_id": "switch.d"}),
    ]

    coordinator._grid_samples += 1
    coordinator._planner_measurement_ages = lambda moment: (1, 1, 100, 1, 1)
    asyncio.run(
        NexusCoordinator._async_control(
            coordinator, data, legacy, (100.0, 100.0, 100.0), 0.0, (3000.0, 3000.0)
        )
    )
    assert _calls(hass)[-2:] == [
        ("switch", "turn_off", {"entity_id": "switch.c"}),
        ("switch", "turn_off", {"entity_id": "switch.d"}),
    ]


def test_offline_watchdog_stops_after_reports_expire_without_grid_event(monkeypatch):
    start = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    clock = [start]
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: clock[0])
    actuator, hass = _actuator()
    slot = {
        "start": start.isoformat(),
        "forecast_start": start.isoformat(),
        "duration_hours": 0.25,
        "price_eur_kwh": 0.50,
        "export_tax_eur_kwh": 0.11,
        "power_w": 5000.0,
        "discharge_mode": "export",
    }
    coordinator = SimpleNamespace(
        experimental_quarter_plan={
            "generated_at": start.isoformat(),
            "minimum_earnings_eur_kwh": 0.15,
            "slots": [slot],
        },
        _quarter_status="experimental",
        settings={
            SET_CONTROL_ENABLED: True,
            SET_DIRECTION_DELAY: 60,
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: False,
            SET_TAX: 0.11,
            SET_PLANNER_OVERRIDE: True,
        },
        _manual_mode=None,
        _controlled_samples=0,
        _grid_samples=1,
        _control_status="enabled",
        _control_was_on=False,
        _pid_memory=None,
        sample_estimator=SimpleNamespace(interval_s=10),
        price_forecast=[(start, 0.50)],
        price_forecast_taxes={},
        _planner_measurement_ages=lambda moment: ((moment - start).total_seconds(),) * 5,
        _get_actuator=lambda: actuator,
    )
    coordinator._planner_action = lambda *args: NexusCoordinator._planner_action(coordinator, *args)
    data = {CONF_BATTERY_SOC: 80.0, CONF_ELECTRICITY_PRICE: 0.50}
    legacy = DispatchPlan("idle", 0, "no_dispatch_condition")

    asyncio.run(
        NexusCoordinator._async_control(
            coordinator, data, legacy, (100.0, 100.0, 100.0), 0.0, (3000.0, 3000.0)
        )
    )
    assert _calls(hass)[-1] == ("switch", "turn_on", {"entity_id": "switch.d"})

    clock[0] += timedelta(seconds=31)
    asyncio.run(
        NexusCoordinator._async_control(
            coordinator, data, legacy, (100.0, 100.0, 100.0), 0.0, (3000.0, 3000.0)
        )
    )
    assert _calls(hass)[-2:] == [
        ("switch", "turn_off", {"entity_id": "switch.c"}),
        ("switch", "turn_off", {"entity_id": "switch.d"}),
    ]


def test_manual_override_keeps_priority_over_planner():
    actuator = SimpleNamespace(async_apply=AsyncMock(return_value="mode_started"))
    coordinator = SimpleNamespace(
        settings={
            SET_CONTROL_ENABLED: True,
            SET_DIRECTION_DELAY: 60,
            "planner_override_enabled": True,
        },
        _manual_mode="charge",
        _manual_until=float("inf"),
        _manual_command=AsyncMock(
            return_value=(DispatchPlan("charge", 1000, "manual_override"), "manual_override")
        ),
        _controlled_samples=0,
        _grid_samples=1,
        _control_status="enabled",
        _control_was_on=False,
        _pid_memory=None,
        sample_estimator=SimpleNamespace(interval_s=10),
        _planner_action=lambda *args: DispatchPlan("idle", 0, "planner_unavailable_or_stale"),
        _get_actuator=lambda: actuator,
    )

    asyncio.run(
        NexusCoordinator._async_control(
            coordinator,
            {},
            DispatchPlan("idle", 0, "no_dispatch_condition"),
            (100.0, 100.0, 100.0),
            0.0,
            (5000.0, 5000.0),
        )
    )

    assert actuator.async_apply.await_args.args[:2] == ("charge", 1000)


def test_planner_toggle_does_not_enable_battery_control():
    coordinator = SimpleNamespace(
        settings={SET_CONTROL_ENABLED: False, SET_PLANNER_OVERRIDE: False},
        _grid_samples=4,
        _controlled_samples=4,
        async_refresh=AsyncMock(),
    )

    asyncio.run(NexusCoordinator.async_set_planner_override(coordinator, True))

    assert coordinator.settings[SET_PLANNER_OVERRIDE] is True
    assert coordinator.settings[SET_CONTROL_ENABLED] is False
    assert coordinator._controlled_samples == 3
    coordinator.async_refresh.assert_awaited_once()


def test_planner_switch_does_not_restore_and_forwards_user_choice():
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id="test"),
        settings={SET_PLANNER_OVERRIDE: False},
        async_set_planner_override=AsyncMock(),
    )
    switch = NexusPlannerSwitch(coordinator)

    assert switch.is_on is False
    assert not isinstance(switch, RestoreEntity)
    asyncio.run(switch.async_turn_on())
    coordinator.async_set_planner_override.assert_awaited_once_with(True)


def test_planner_override_defaults_off_on_new_coordinator():
    coordinator = SimpleNamespace(entry=SimpleNamespace(data={}, options={}))

    settings = NexusCoordinator._initial_settings(coordinator)

    assert settings[SET_CONTROL_ENABLED] is False
    assert settings[SET_PLANNER_OVERRIDE] is False


def test_disabling_battery_control_also_clears_planner_override():
    coordinator = SimpleNamespace(
        settings={SET_CONTROL_ENABLED: True, SET_PLANNER_OVERRIDE: True},
        _manual_mode=None,
        _async_release=AsyncMock(),
        async_refresh=AsyncMock(),
    )

    asyncio.run(NexusCoordinator.async_set_control(coordinator, False))

    assert coordinator.settings[SET_CONTROL_ENABLED] is False
    assert coordinator.settings[SET_PLANNER_OVERRIDE] is False
    coordinator._async_release.assert_awaited_once()


def test_control_error_disables_planner_override():
    actuator = SimpleNamespace(
        async_apply=AsyncMock(side_effect=actuator_module.ActuatorError("error"))
    )
    coordinator = SimpleNamespace(
        settings={SET_CONTROL_ENABLED: True, SET_DIRECTION_DELAY: 60, SET_PLANNER_OVERRIDE: True},
        _manual_mode=None,
        _controlled_samples=0,
        _grid_samples=1,
        _control_status="enabled",
        _control_was_on=False,
        _pid_memory=None,
        sample_estimator=SimpleNamespace(interval_s=10),
        _planner_action=lambda *args: DispatchPlan("charge", 1000, "planned_grid_charge"),
        _get_actuator=lambda: actuator,
        _async_release=AsyncMock(),
    )

    asyncio.run(
        NexusCoordinator._async_control(
            coordinator,
            {},
            DispatchPlan("idle", 0, "no_dispatch_condition"),
            (100.0, 100.0, 100.0),
            0.0,
            (5000.0, 5000.0),
        )
    )

    assert coordinator.settings[SET_CONTROL_ENABLED] is False
    assert coordinator.settings[SET_PLANNER_OVERRIDE] is False
    coordinator._async_release.assert_awaited_once()


def test_solar_pid_is_bounded_by_planned_power_during_override():
    actuator = SimpleNamespace(async_apply=AsyncMock(return_value="power_updated"))
    received_limits = []

    def pid(plan, grid, battery, limits):
        received_limits.append(limits)
        return DispatchPlan("charge", limits[0], "closed_loop_pid_step")

    coordinator = SimpleNamespace(
        settings={
            SET_CONTROL_ENABLED: True,
            SET_DIRECTION_DELAY: 60,
            SET_MIN_CONTROL_POWER: 0,
            SET_DISCHARGE_OFFSET: 0,
            "planner_override_enabled": True,
        },
        _manual_mode=None,
        _controlled_samples=0,
        _grid_samples=1,
        _control_status="enabled",
        _control_was_on=False,
        _pid_memory=None,
        sample_estimator=SimpleNamespace(interval_s=10),
        _planner_action=lambda *args: DispatchPlan("charge", 500, "pv_surplus_reduce_export"),
        _pid_command=pid,
        _get_actuator=lambda: actuator,
    )

    asyncio.run(
        NexusCoordinator._async_control(
            coordinator,
            {},
            DispatchPlan("idle", 0, "no_dispatch_condition"),
            (-1000.0, -1000.0, -1000.0),
            0.0,
            (5000.0, 5000.0),
        )
    )

    assert received_limits == [(500.0, 5000.0)]
    assert actuator.async_apply.await_args.args[:2] == ("charge", 500.0)


def test_planner_caps_pid_deadband_hold_even_if_battery_was_above_target():
    actuator = SimpleNamespace(async_apply=AsyncMock(return_value="power_updated"))
    coordinator = SimpleNamespace(
        settings={
            SET_CONTROL_ENABLED: True,
            SET_DIRECTION_DELAY: 60,
            SET_MIN_CONTROL_POWER: 0,
            SET_DISCHARGE_OFFSET: 0,
            "planner_override_enabled": True,
        },
        _manual_mode=None,
        _controlled_samples=0,
        _grid_samples=1,
        _control_status="enabled",
        _control_was_on=False,
        _pid_memory=None,
        sample_estimator=SimpleNamespace(interval_s=10),
        _planner_action=lambda *args: DispatchPlan("charge", 500, "pv_surplus_reduce_export"),
        _pid_command=lambda *args: DispatchPlan("charge", 2000, "deadband_hold"),
        _get_actuator=lambda: actuator,
    )

    asyncio.run(
        NexusCoordinator._async_control(
            coordinator,
            {},
            DispatchPlan("idle", 0, "no_dispatch_condition"),
            (-1000.0, -1000.0, -1000.0),
            0.0,
            (5000.0, 5000.0),
        )
    )

    assert actuator.async_apply.await_args.args[:2] == ("charge", 500)


def test_diagnostic_planning_is_backgrounded_and_does_not_duplicate_work():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        coordinator = SimpleNamespace(
            _quarter_task=None,
            _quarter_last_start=None,
            _quarter_status="unavailable",
            hass=SimpleNamespace(async_create_task=asyncio.create_task),
        )

        async def run(data, now, limits):
            data["separate_snapshot"] = True
            started.set()
            await release.wait()
            coordinator._quarter_status = "experimental"

        coordinator._async_run_experimental_quarter_plan = run
        data = {"control_status": "unchanged"}
        now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
        NexusCoordinator._schedule_experimental_quarter_plan(coordinator, data, now, (5000, 5000))
        task = coordinator._quarter_task
        await started.wait()
        assert not task.done()
        assert "separate_snapshot" not in data

        NexusCoordinator._schedule_experimental_quarter_plan(coordinator, data, now, (5000, 5000))
        assert coordinator._quarter_task is task
        release.set()
        await task
        NexusCoordinator._schedule_experimental_quarter_plan(coordinator, data, now, (5000, 5000))
        assert coordinator._quarter_task is task
        assert data["experimental_quarter_plan"] == "experimental"

    asyncio.run(scenario())


def test_diagnostic_planning_runs_every_five_minutes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(coordinator_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def scenario():
        run = AsyncMock()
        coordinator = SimpleNamespace(
            _quarter_task=None,
            _quarter_last_start=None,
            _quarter_status="unavailable",
            _async_run_experimental_quarter_plan=run,
            hass=SimpleNamespace(async_create_task=asyncio.create_task),
        )
        now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
        NexusCoordinator._schedule_experimental_quarter_plan(coordinator, {}, now, (5000, 5000))
        await coordinator._quarter_task

        clock[0] += 299
        NexusCoordinator._schedule_experimental_quarter_plan(coordinator, {}, now, (5000, 5000))
        assert run.await_count == 1

        clock[0] += 1
        NexusCoordinator._schedule_experimental_quarter_plan(coordinator, {}, now, (5000, 5000))
        await coordinator._quarter_task
        assert run.await_count == 2

    asyncio.run(scenario())


def test_diagnostic_background_failure_is_fail_closed():
    async def fail(data, now, limits):
        raise RuntimeError("forecast failure")

    coordinator = SimpleNamespace(
        experimental_quarter_plan={"slots": ["stale"]},
        _quarter_status="experimental",
        _async_experimental_quarter_plan=fail,
    )
    asyncio.run(
        NexusCoordinator._async_run_experimental_quarter_plan(
            coordinator, {}, datetime(2026, 11, 9, tzinfo=UTC), (5000, 5000)
        )
    )

    assert coordinator.experimental_quarter_plan == {}
    assert coordinator._quarter_status == "unavailable"


def test_diagnostic_keeps_previous_plan_while_recalculation_is_running():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        old_plan = {"slots": ["old"]}
        coordinator = SimpleNamespace(
            experimental_quarter_plan=old_plan,
            _quarter_status="experimental",
        )

        async def recalculate(data, now, limits):
            started.set()
            await release.wait()
            coordinator.experimental_quarter_plan = {"slots": ["new"]}
            data["experimental_quarter_plan"] = "experimental"

        coordinator._async_experimental_quarter_plan = recalculate
        task = asyncio.create_task(
            NexusCoordinator._async_run_experimental_quarter_plan(
                coordinator, {}, datetime(2026, 11, 9, tzinfo=UTC), (5000, 5000)
            )
        )
        await started.wait()
        assert coordinator.experimental_quarter_plan == old_plan
        release.set()
        await task
        assert coordinator.experimental_quarter_plan == {"slots": ["new"]}

    asyncio.run(scenario())


def test_real_diagnostic_does_not_clear_valid_plan_before_executor_finishes():
    async def scenario():
        now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
        start = now.replace(minute=15)
        started = asyncio.Event()
        release = asyncio.Event()
        old_plan = {"slots": ["old"]}

        async def executor(function):
            started.set()
            await release.wait()
            return function()

        coordinator = SimpleNamespace(
            experimental_quarter_plan=old_plan,
            price_forecast=[(start, 0.20), (start + timedelta(minutes=15), 0.70)],
            price_forecast_taxes={},
            settings={
                SET_MIN_DISCHARGE_SOC: 10,
                SET_MIN_EARNINGS: 0.15,
                SET_EXPORT_INCLUDES_TAX: True,
            },
            tracker=SimpleNamespace(
                state=SimpleNamespace(avg_daytime_load_w=500.0, stored_value=0.30)
            ),
            hass=SimpleNamespace(async_add_executor_job=executor),
            _experimental_solar_quarters=lambda now: {},
        )
        data = {CONF_BATTERY_SOC: 50.0, "tax_used": 0.11}
        task = asyncio.create_task(
            NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (5000, 5000))
        )
        await started.wait()
        try:
            assert coordinator.experimental_quarter_plan == old_plan
        finally:
            release.set()
            await task

    asyncio.run(scenario())


def test_diagnostic_only_uses_complete_solar_forecast():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    start = now.replace(minute=15)
    second = start + timedelta(minutes=15)

    async def executor(function):
        return function()

    coordinator = SimpleNamespace(
        price_forecast=[(start, 0.20), (second, 0.70)],
        price_forecast_taxes={},
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: False,
            "tax_per_kwh": 0.11,
        },
        tracker=SimpleNamespace(state=SimpleNamespace(avg_daytime_load_w=0.0, stored_value=1.0)),
        hass=SimpleNamespace(async_add_executor_job=executor),
        _experimental_solar_quarters=lambda now: {start: 1.25, second: 0.0},
    )
    data = {CONF_BATTERY_SOC: 50.0, "tax_used": 0.11}
    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (5000, 5000))
    )
    complete = coordinator.experimental_quarter_plan

    coordinator._experimental_solar_quarters = lambda now: {start: 1.25}
    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (5000, 5000))
    )
    incomplete = coordinator.experimental_quarter_plan

    assert complete["solar_included"] is True
    assert incomplete["solar_included"] is False
    assert complete["estimated_profit_eur"] > incomplete["estimated_profit_eur"]
    assert incomplete["slots"][1]["solar_forecast_kwh"] is None


def test_diagnostic_scales_current_quarter_solar_to_remaining_time():
    now = datetime(2026, 11, 9, 12, 7, 30, tzinfo=UTC)
    start = now.replace(minute=0, second=0)
    second = start + timedelta(minutes=15)

    async def executor(function):
        return function()

    coordinator = SimpleNamespace(
        price_forecast=[(start, 0.20), (second, 0.70)],
        price_forecast_taxes={},
        settings={
            SET_MIN_DISCHARGE_SOC: 10,
            SET_MIN_EARNINGS: 0.15,
            SET_EXPORT_INCLUDES_TAX: True,
        },
        tracker=SimpleNamespace(state=SimpleNamespace(avg_daytime_load_w=0.0, stored_value=1.0)),
        hass=SimpleNamespace(async_add_executor_job=executor),
        _experimental_solar_quarters=lambda now: {start: 1.0, second: 0.0},
    )
    data = {CONF_BATTERY_SOC: 50.0, "tax_used": 0.11}

    asyncio.run(
        NexusCoordinator._async_experimental_quarter_plan(coordinator, data, now, (5000, 5000))
    )

    first = coordinator.experimental_quarter_plan["slots"][0]
    assert first["start"] == now.isoformat()
    assert first["forecast_start"] == start.isoformat()
    assert first["duration_hours"] == pytest.approx(0.125)
    assert first["solar_forecast_kwh"] == pytest.approx(0.5)


def test_solar_diagnostic_reads_selected_local_today_and_tomorrow():
    now = datetime(2026, 11, 9, 18, 30, tzinfo=UTC)
    entities = {
        CONF_SOLAR_HOURLY_TODAY: "sensor.selected_today",
        CONF_SOLAR_HOURLY_TOMORROW: "sensor.selected_tomorrow",
    }
    states = {
        "sensor.selected_today": SimpleNamespace(
            state="10.91",
            attributes={"unit_of_measurement": "kWh", "hourly_forecast": {"20:00": 0.4}},
        ),
        "sensor.selected_tomorrow": SimpleNamespace(
            state="4.24", attributes={"unit_of_measurement": "kWh", "hours": {"08:00": 0.102}}
        ),
    }
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Amsterdam"), states=states),
        configured_entity_id=entities.get,
    )

    quarters = NexusCoordinator._experimental_solar_quarters(coordinator, now)

    assert quarters[datetime(2026, 11, 9, 19, tzinfo=UTC)] == pytest.approx(0.1)
    assert quarters[datetime(2026, 11, 10, 7, tzinfo=UTC)] == pytest.approx(0.102 / 4)
    assert len(quarters) == 8


def test_second_cycle_only_when_it_is_profitable():
    settings = {
        "capacity_kwh": 20.0,
        "initial_soc_pct": 0.0,
        "min_soc_pct": 0.0,
        "max_power_w": 10_000.0,
        "efficiency": 1.0,
        "min_profit_eur_per_kwh": 0.15,
        "export_tax_eur_per_kwh": 0.0,
    }
    two = plan_quarters([price for price in (0.40, 0.60, 0.55, 0.80) for _ in range(8)], **settings)
    one = plan_quarters([price for price in (0.40, 0.60, 0.55, 0.65) for _ in range(8)], **settings)

    assert two.profit_eur == pytest.approx(9.0)
    assert one.profit_eur == pytest.approx(5.0)
    assert all(power == 0 for power in one.power_w[8:24])
