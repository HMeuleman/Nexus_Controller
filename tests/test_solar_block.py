"""Tests for the solar production limit on discharging."""

from custom_components.nexus_controller.dispatch import DispatchSettings, plan_battery_dispatch


def _plan(inhibit_w):
    return plan_battery_dispatch(
        phase_imports_w=(200.0, 200.0, 200.0),
        battery_soc_pct=80.0,
        electricity_price=0.50,
        pv_power_w=20.0,
        max_charge_power_w=5000.0,
        max_discharge_power_w=5000.0,
        settings=DispatchSettings(
            0.15, 0.25, 10.0, 100.0, solar_inhibit_discharge_above_w=inhibit_w
        ),
    )


def test_a_trace_of_solar_blocks_discharge_only_when_the_limit_is_zero():
    assert _plan(0.0).mode == "idle"
    assert _plan(10_000.0).mode == "discharge"
