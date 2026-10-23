"""Offline closed-loop power regulation simulation."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .dispatch import (
    DEFAULT_DISPATCH_SETTINGS,
    DispatchMode,
    DispatchPlan,
    DispatchSettings,
    plan_battery_dispatch,
)


@dataclass(frozen=True)
class PIDTuning:
    """Explicit testable tuning values; these are not live-control defaults."""

    kp_charge: float
    ki_charge: float
    kd_charge: float
    kp_discharge: float
    ki_discharge: float
    kd_discharge: float
    integral_limit_ws: float
    filter_alpha: float
    start_error_w: float = 15.0
    stop_error_w: float = 8.0


@dataclass(frozen=True)
class PIDMemory:
    """State carried from one simulated control interval to the next."""

    active_mode: DispatchMode = "idle"
    integral_error_ws: float = 0.0
    previous_error_w: float | None = None
    filtered_battery_power_w: float = 0.0


@dataclass(frozen=True)
class PIDStepResult:
    """A proposed output and updated simulation state, with no device writes."""

    plan: DispatchPlan
    memory: PIDMemory


def simulate_dispatch_cycle(
    *,
    phase_imports_w: tuple[float | None, float | None, float | None],
    battery_soc_pct: float | None,
    electricity_price: float | None,
    pv_power_w: float | None,
    actual_charge_power_w: float | None,
    actual_discharge_power_w: float | None,
    max_charge_power_w: float | None,
    max_discharge_power_w: float | None,
    interval_seconds: float,
    tuning: PIDTuning,
    settings: DispatchSettings = DEFAULT_DISPATCH_SETTINGS,
    memory: PIDMemory | None = None,
) -> PIDStepResult:
    """Simulate permission arbitration followed by the allowed closed-loop step.

    Cheap-price grid charging is returned as a separate direct proposal; it does
    not run the fast PID loop. This mirrors the separate net-charge module in the
    reference EMS. No Home Assistant calls or inverter writes are made.
    """
    memory = memory or PIDMemory()
    decision = plan_battery_dispatch(
        phase_imports_w=phase_imports_w,
        battery_soc_pct=battery_soc_pct,
        electricity_price=electricity_price,
        pv_power_w=pv_power_w,
        max_charge_power_w=max_charge_power_w,
        max_discharge_power_w=max_discharge_power_w,
        settings=settings,
    )

    actual_measurements = (actual_charge_power_w, actual_discharge_power_w)
    if any(value is None or not math.isfinite(value) for value in actual_measurements):
        return _idle_result("actual_battery_power_unavailable")

    if decision.mode == "idle":
        return PIDStepResult(decision, PIDMemory())
    if decision.reason == "low_price_charge":
        return PIDStepResult(decision, PIDMemory())

    net_import_w = sum(phase for phase in phase_imports_w if phase is not None)
    return simulate_closed_loop_step(
        net_import_w=net_import_w,
        actual_charge_power_w=actual_charge_power_w,
        actual_discharge_power_w=actual_discharge_power_w,
        permitted_mode=decision.mode,
        max_charge_power_w=max_charge_power_w,
        max_discharge_power_w=max_discharge_power_w,
        interval_seconds=interval_seconds,
        tuning=tuning,
        memory=memory,
    )


def simulate_closed_loop_step(
    *,
    net_import_w: float | None,
    actual_charge_power_w: float | None,
    actual_discharge_power_w: float | None,
    permitted_mode: DispatchMode,
    max_charge_power_w: float | None,
    max_discharge_power_w: float | None,
    interval_seconds: float,
    tuning: PIDTuning,
    memory: PIDMemory | None = None,
) -> PIDStepResult:
    """Simulate one PID update using measured grid and battery power.

    Positive net power means import. Actual battery power is positive for
    discharge and negative for charge. The chosen mode must be granted by the
    separate decision layer before this function can produce a non-idle plan.
    """
    memory = memory or PIDMemory()
    measurements = (
        net_import_w,
        actual_charge_power_w,
        actual_discharge_power_w,
        max_charge_power_w,
        max_discharge_power_w,
        interval_seconds,
    )
    if any(value is None or not math.isfinite(value) for value in measurements):
        return _idle_result("input_unavailable")

    assert net_import_w is not None
    assert actual_charge_power_w is not None
    assert actual_discharge_power_w is not None
    assert max_charge_power_w is not None
    assert max_discharge_power_w is not None

    if (
        permitted_mode not in ("idle", "charge", "discharge")
        or max_charge_power_w < 0
        or max_discharge_power_w < 0
        or actual_charge_power_w < 0
        or actual_discharge_power_w < 0
        or interval_seconds <= 0
        or not _valid_tuning(tuning)
    ):
        return _idle_result("invalid_inputs_or_tuning")

    if permitted_mode == "idle":
        return _idle_result("decision_layer_blocked")

    if (permitted_mode == "discharge" and net_import_w <= 0) or (
        permitted_mode == "charge" and net_import_w >= 0
    ):
        return _idle_result("power_direction_conflicts_with_permission")

    mode_is_continuing = memory.active_mode == permitted_mode
    required_error_w = tuning.stop_error_w if mode_is_continuing else tuning.start_error_w
    if abs(net_import_w) <= required_error_w:
        return _idle_result("hysteresis_deadband")

    actual_battery_power_w = actual_discharge_power_w - actual_charge_power_w
    proportional_gain, integral_gain, derivative_gain = _mode_gains(permitted_mode, tuning)
    next_integral_ws = memory.integral_error_ws if mode_is_continuing else 0.0
    next_integral_ws = _clamp(
        next_integral_ws + net_import_w * interval_seconds,
        -tuning.integral_limit_ws,
        tuning.integral_limit_ws,
    )
    derivative_w_per_second = (
        (net_import_w - memory.previous_error_w) / interval_seconds
        if mode_is_continuing and memory.previous_error_w is not None
        else 0.0
    )

    unconstrained_power_w = (
        actual_battery_power_w
        + proportional_gain * net_import_w
        + integral_gain * next_integral_ws
        + derivative_gain * derivative_w_per_second
    )
    lower_limit_w, upper_limit_w = _mode_limits(
        permitted_mode, max_charge_power_w, max_discharge_power_w
    )
    constrained_power_w = _clamp(unconstrained_power_w, lower_limit_w, upper_limit_w)

    if _integral_pushes_further_into_saturation(
        unconstrained_power_w,
        lower_limit_w,
        upper_limit_w,
        net_import_w,
    ):
        next_integral_ws = memory.integral_error_ws if mode_is_continuing else 0.0
        unconstrained_power_w = (
            actual_battery_power_w
            + proportional_gain * net_import_w
            + integral_gain * next_integral_ws
            + derivative_gain * derivative_w_per_second
        )
        constrained_power_w = _clamp(unconstrained_power_w, lower_limit_w, upper_limit_w)

    previous_filtered_power_w = memory.filtered_battery_power_w if mode_is_continuing else 0.0
    filtered_power_w = (
        tuning.filter_alpha * constrained_power_w
        + (1 - tuning.filter_alpha) * previous_filtered_power_w
    )
    filtered_power_w = _clamp(filtered_power_w, lower_limit_w, upper_limit_w)
    next_memory = PIDMemory(
        active_mode=permitted_mode,
        integral_error_ws=next_integral_ws,
        previous_error_w=net_import_w,
        filtered_battery_power_w=filtered_power_w,
    )

    if abs(filtered_power_w) <= tuning.stop_error_w:
        plan = DispatchPlan("idle", 0.0, "output_deadband")
    else:
        plan = DispatchPlan(
            permitted_mode,
            abs(filtered_power_w),
            "closed_loop_pid_step",
        )
    return PIDStepResult(plan, next_memory)


def _valid_tuning(tuning: PIDTuning) -> bool:
    """Return whether controller gains and damping settings are usable."""
    values = (
        tuning.kp_charge,
        tuning.ki_charge,
        tuning.kd_charge,
        tuning.kp_discharge,
        tuning.ki_discharge,
        tuning.kd_discharge,
        tuning.integral_limit_ws,
        tuning.filter_alpha,
        tuning.start_error_w,
        tuning.stop_error_w,
    )
    return (
        all(math.isfinite(value) and value >= 0 for value in values)
        and tuning.integral_limit_ws > 0
        and 0 < tuning.filter_alpha <= 1
        and tuning.start_error_w > tuning.stop_error_w
    )


def _mode_gains(mode: DispatchMode, tuning: PIDTuning) -> tuple[float, float, float]:
    """Return independent gains for the permitted direction."""
    if mode == "charge":
        return tuning.kp_charge, tuning.ki_charge, tuning.kd_charge
    return tuning.kp_discharge, tuning.ki_discharge, tuning.kd_discharge


def _mode_limits(
    mode: DispatchMode,
    max_charge_power_w: float,
    max_discharge_power_w: float,
) -> tuple[float, float]:
    """Return signed battery power limits for the permitted direction."""
    if mode == "charge":
        return -max_charge_power_w, 0.0
    return 0.0, max_discharge_power_w


def _integral_pushes_further_into_saturation(
    requested_power_w: float,
    lower_limit_w: float,
    upper_limit_w: float,
    net_import_w: float,
) -> bool:
    """Detect integral accumulation that would drive a saturated output farther."""
    return (requested_power_w > upper_limit_w and net_import_w > 0) or (
        requested_power_w < lower_limit_w and net_import_w < 0
    )


def _clamp(value: float, lower: float, upper: float) -> float:
    """Clamp a numeric output to an inclusive range."""
    return min(max(value, lower), upper)


def _idle_result(reason: str) -> PIDStepResult:
    """Return an idle plan and reset controller memory after a safety stop."""
    return PIDStepResult(
        DispatchPlan("idle", 0.0, reason),
        PIDMemory(),
    )
