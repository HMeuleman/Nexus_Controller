"""Offline tests for the pure battery dispatch planner."""

import pytest

from custom_components.nexus_controller.dispatch import DispatchSettings, plan_battery_dispatch

SETTINGS = DispatchSettings(
    charge_price_below=0.10,
    discharge_price_above=0.30,
    min_soc_pct=15,
    max_soc_pct=90,
)


def plan(
    phases: tuple[float | None, float | None, float | None] = (500, 500, 500),
    soc: float | None = 50,
    price: float | None = 0.20,
    pv: float | None = 0,
    charge_cap: float | None = 6000,
    discharge_cap: float | None = 6000,
    settings: DispatchSettings = SETTINGS,
):
    """Build a plan with representative valid input defaults."""
    return plan_battery_dispatch(
        phase_imports_w=phases,
        battery_soc_pct=soc,
        electricity_price=price,
        pv_power_w=pv,
        max_charge_power_w=charge_cap,
        max_discharge_power_w=discharge_cap,
        settings=settings,
    )


def test_pv_surplus_charges_only_enough_to_approach_zero_grid_power():
    """Solar export is absorbed without exceeding the phase-derived charge cap."""
    result = plan(phases=(-1200, -800, -500), charge_cap=4000)

    assert (result.mode, result.power_w, result.reason) == (
        "charge",
        2500,
        "pv_surplus_reduce_export",
    )


def test_low_price_charging_uses_phase_limited_power():
    """Cheap-price charging is limited by the previously calculated safe cap."""
    result = plan(price=0.05, charge_cap=3200)

    assert (result.mode, result.power_w) == ("charge", 3200)


def test_high_price_without_solar_discharges_toward_zero_import():
    """Expensive power is offset toward net zero, but never above phase limits."""
    result = plan(phases=(5000, 5000, -4000), price=0.35, discharge_cap=2250)

    assert (result.mode, result.power_w, result.reason) == (
        "discharge",
        2250,
        "high_price_no_solar_reduce_import",
    )


def test_high_price_discharge_is_inhibited_when_pv_is_available():
    """High price alone must not discharge while PV exceeds the configured inhibit threshold."""
    result = plan(price=0.35, pv=500, phases=(1000, 1000, 1000))

    assert result.mode == "idle"


@pytest.mark.parametrize(
    ("soc", "price", "expected_mode"),
    [(15, 0.35, "idle"), (90, 0.05, "idle")],
)
def test_soc_boundaries_prevent_discharge_or_charge(soc, price, expected_mode):
    """Configured SOC reserve and ceiling are respected at their boundary."""
    assert plan(soc=soc, price=price).mode == expected_mode


def test_missing_phase_or_other_measurement_fails_idle():
    """Missing data never produces a proposed battery action."""
    assert plan(phases=(500, None, 500)).mode == "idle"
    assert plan(pv=None).mode == "idle"
    assert plan(discharge_cap=None, price=0.35).mode == "idle"


def test_invalid_price_bands_fail_idle():
    """Overlapping price bands are rejected instead of choosing an arbitrary action."""
    invalid_settings = DispatchSettings(
        charge_price_below=0.30,
        discharge_price_above=0.10,
        min_soc_pct=15,
        max_soc_pct=90,
    )

    result = plan(price=0.20, settings=invalid_settings)

    assert (result.mode, result.reason) == ("idle", "invalid_settings_or_limits")
