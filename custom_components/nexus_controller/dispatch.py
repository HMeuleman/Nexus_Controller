"""Offline-only battery dispatch planning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

DispatchMode = Literal["idle", "charge", "discharge"]


@dataclass(frozen=True)
class DispatchSettings:
    """Strategy thresholds for a dispatch plan."""

    charge_price_below: float
    discharge_price_above: float
    min_soc_pct: float
    max_soc_pct: float
    solar_inhibit_discharge_above_w: float = 0.0


@dataclass(frozen=True)
class DispatchPlan:
    """Proposed battery mode and magnitude, without issuing any device command."""

    mode: DispatchMode
    power_w: float
    reason: str


DEFAULT_DISPATCH_SETTINGS = DispatchSettings(
    charge_price_below=0.15,
    discharge_price_above=0.35,
    min_soc_pct=7.0,
    max_soc_pct=100.0,
)


def plan_battery_dispatch(
    *,
    phase_imports_w: tuple[float | None, float | None, float | None],
    battery_soc_pct: float | None,
    electricity_price: float | None,
    pv_power_w: float | None,
    max_charge_power_w: float | None,
    max_discharge_power_w: float | None,
    settings: DispatchSettings = DEFAULT_DISPATCH_SETTINGS,
) -> DispatchPlan:
    """Propose a safe capped response to PV surplus and configured price bands.

    Positive phase values mean grid import; negative values mean export. Power in
    the returned plan is a non-negative magnitude. This function is intentionally
    side-effect-free and does not issue Home Assistant service calls.
    """
    measurements = (
        *phase_imports_w,
        battery_soc_pct,
        electricity_price,
        pv_power_w,
        max_charge_power_w,
        max_discharge_power_w,
    )
    if any(value is None or not math.isfinite(value) for value in measurements):
        return DispatchPlan("idle", 0.0, "input_unavailable")

    if (
        not math.isfinite(settings.charge_price_below)
        or not math.isfinite(settings.discharge_price_above)
        or not math.isfinite(settings.min_soc_pct)
        or not math.isfinite(settings.max_soc_pct)
        or not math.isfinite(settings.solar_inhibit_discharge_above_w)
        or settings.charge_price_below >= settings.discharge_price_above
        or not 0 <= settings.min_soc_pct < settings.max_soc_pct <= 100
        or settings.solar_inhibit_discharge_above_w < 0
        or max_charge_power_w < 0
        or max_discharge_power_w < 0
    ):
        return DispatchPlan("idle", 0.0, "invalid_settings_or_limits")

    assert battery_soc_pct is not None
    assert electricity_price is not None
    assert pv_power_w is not None
    assert max_charge_power_w is not None
    assert max_discharge_power_w is not None
    total_grid_import_w = sum(phase for phase in phase_imports_w if phase is not None)

    if (
        electricity_price >= settings.discharge_price_above
        and pv_power_w <= settings.solar_inhibit_discharge_above_w
        and total_grid_import_w > 0
        and battery_soc_pct > settings.min_soc_pct
    ):
        power_w = min(total_grid_import_w, max_discharge_power_w)
        if power_w > 0:
            return DispatchPlan("discharge", power_w, "high_price_no_solar_reduce_import")

    if electricity_price <= settings.charge_price_below and battery_soc_pct < settings.max_soc_pct:
        if max_charge_power_w > 0:
            return DispatchPlan("charge", max_charge_power_w, "low_price_charge")

    if total_grid_import_w < 0 and battery_soc_pct < settings.max_soc_pct:
        power_w = min(-total_grid_import_w, max_charge_power_w)
        if power_w > 0:
            return DispatchPlan("charge", power_w, "pv_surplus_reduce_export")

    return DispatchPlan("idle", 0.0, "no_dispatch_condition")
