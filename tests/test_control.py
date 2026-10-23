"""Tests for the offline closed-loop power regulation step."""

import pytest

from custom_components.nexus_controller.control import (
    PIDMemory,
    PIDTuning,
    simulate_closed_loop_step,
    simulate_dispatch_cycle,
)
from custom_components.nexus_controller.dispatch import DEFAULT_DISPATCH_SETTINGS


def tuning(
    *,
    kp_charge: float = 0.5,
    ki_charge: float = 0.0,
    kd_charge: float = 0.0,
    kp_discharge: float = 0.5,
    ki_discharge: float = 0.0,
    kd_discharge: float = 0.0,
    integral_limit_ws: float = 10_000,
    filter_alpha: float = 1.0,
    start_error_w: float = 15.0,
    stop_error_w: float = 8.0,
) -> PIDTuning:
    """Use explicit simulation-only tuning values."""
    return PIDTuning(
        kp_charge=kp_charge,
        ki_charge=ki_charge,
        kd_charge=kd_charge,
        kp_discharge=kp_discharge,
        ki_discharge=ki_discharge,
        kd_discharge=kd_discharge,
        integral_limit_ws=integral_limit_ws,
        filter_alpha=filter_alpha,
        start_error_w=start_error_w,
        stop_error_w=stop_error_w,
    )


def test_pid_corrects_from_measured_battery_output_not_zero():
    """Measured 850 W discharge plus residual import gives a 925 W proposal."""
    result = simulate_closed_loop_step(
        net_import_w=150,
        actual_charge_power_w=0,
        actual_discharge_power_w=850,
        permitted_mode="discharge",
        max_charge_power_w=4000,
        max_discharge_power_w=4000,
        interval_seconds=1,
        tuning=tuning(kp_discharge=0.5),
    )

    assert (result.plan.mode, result.plan.power_w) == ("discharge", 925)


def test_charge_and_discharge_use_independent_proportional_gains():
    """The charging gain is independent from the discharging gain."""
    result = simulate_closed_loop_step(
        net_import_w=-200,
        actual_charge_power_w=100,
        actual_discharge_power_w=0,
        permitted_mode="charge",
        max_charge_power_w=1000,
        max_discharge_power_w=1000,
        interval_seconds=1,
        tuning=tuning(kp_charge=0.25, kp_discharge=0.9),
    )

    assert (result.plan.mode, result.plan.power_w) == ("charge", 150)


def test_pid_output_respects_mode_power_cap():
    """A calculated output is clamped to the phase-safe direction cap."""
    result = simulate_closed_loop_step(
        net_import_w=2000,
        actual_charge_power_w=0,
        actual_discharge_power_w=0,
        permitted_mode="discharge",
        max_charge_power_w=1000,
        max_discharge_power_w=500,
        interval_seconds=1,
        tuning=tuning(kp_discharge=2.0),
    )

    assert (result.plan.mode, result.plan.power_w) == ("discharge", 500)


def test_integrator_does_not_wind_up_against_power_cap():
    """An error pushing farther into saturation does not accumulate integral."""
    result = simulate_closed_loop_step(
        net_import_w=1000,
        actual_charge_power_w=0,
        actual_discharge_power_w=0,
        permitted_mode="discharge",
        max_charge_power_w=1000,
        max_discharge_power_w=500,
        interval_seconds=10,
        tuning=tuning(kp_discharge=1, ki_discharge=0.1),
    )

    assert result.plan.power_w == 500
    assert result.memory.integral_error_ws == 0


def test_filter_smooths_requested_power_changes():
    """A half-weight filter moves the command part-way toward the new PID value."""
    result = simulate_closed_loop_step(
        net_import_w=100,
        actual_charge_power_w=0,
        actual_discharge_power_w=0,
        permitted_mode="discharge",
        max_charge_power_w=1000,
        max_discharge_power_w=1000,
        interval_seconds=1,
        tuning=tuning(kp_discharge=1, filter_alpha=0.5),
    )

    assert result.plan.power_w == 50
    assert result.memory.filtered_battery_power_w == 50


def test_hysteresis_requires_larger_error_to_start_than_to_continue():
    """Start threshold is strict; an active mode may continue above stop threshold."""
    step_args = {
        "actual_charge_power_w": 0,
        "actual_discharge_power_w": 0,
        "permitted_mode": "discharge",
        "max_charge_power_w": 1000,
        "max_discharge_power_w": 1000,
        "interval_seconds": 1,
        "tuning": tuning(kp_discharge=2),
    }
    below_start = simulate_closed_loop_step(net_import_w=10, **step_args)
    started = simulate_closed_loop_step(net_import_w=20, **step_args)
    continuing = simulate_closed_loop_step(
        net_import_w=10,
        memory=started.memory,
        **step_args,
    )
    stopped = simulate_closed_loop_step(
        net_import_w=7,
        memory=continuing.memory,
        **step_args,
    )

    assert below_start.plan.mode == "idle"
    assert started.plan.mode == "discharge"
    assert continuing.plan.mode == "discharge"
    assert stopped.plan.mode == "idle"
    assert stopped.memory.active_mode == "idle"


@pytest.mark.parametrize(
    ("net_import", "actual_charge", "actual_discharge", "mode"),
    [(None, 0, 0, "discharge"), (100, None, 0, "discharge"), (100, 0, 0, "idle")],
)
def test_missing_measurements_or_denied_permission_fail_idle(
    net_import,
    actual_charge,
    actual_discharge,
    mode,
):
    """The controller produces no command if a required input or permission is missing."""
    result = simulate_closed_loop_step(
        net_import_w=net_import,
        actual_charge_power_w=actual_charge,
        actual_discharge_power_w=actual_discharge,
        permitted_mode=mode,
        max_charge_power_w=1000,
        max_discharge_power_w=1000,
        interval_seconds=1,
        tuning=tuning(),
        memory=PIDMemory(active_mode="discharge", integral_error_ws=50),
    )

    assert result.plan.mode == "idle"
    assert result.plan.power_w == 0
    assert result.memory == PIDMemory()


def test_high_price_dispatch_uses_pid_after_decision_layer_permission():
    """The high-price decision grants direction; PID sizes from actual battery output."""
    result = simulate_dispatch_cycle(
        phase_imports_w=(50, 50, 50),
        battery_soc_pct=50,
        electricity_price=0.35,
        pv_power_w=0,
        actual_charge_power_w=0,
        actual_discharge_power_w=850,
        max_charge_power_w=1000,
        max_discharge_power_w=1000,
        interval_seconds=1,
        tuning=tuning(kp_discharge=0.5),
    )

    assert (result.plan.mode, result.plan.power_w) == ("discharge", 925)


def test_low_price_grid_charge_is_a_separate_pid_interlock_action():
    """Cheap grid charging uses its own proposal and clears main-loop PID memory."""
    result = simulate_dispatch_cycle(
        phase_imports_w=(200, 200, 200),
        battery_soc_pct=50,
        electricity_price=0.15,
        pv_power_w=0,
        actual_charge_power_w=0,
        actual_discharge_power_w=0,
        max_charge_power_w=900,
        max_discharge_power_w=900,
        interval_seconds=1,
        tuning=tuning(kp_charge=0.9),
        memory=PIDMemory(active_mode="discharge", integral_error_ws=400),
        settings=DEFAULT_DISPATCH_SETTINGS,
    )

    assert (result.plan.mode, result.plan.power_w, result.plan.reason) == (
        "charge",
        900,
        "low_price_charge",
    )
    assert result.memory == PIDMemory()
