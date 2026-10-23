"""Read-only proposal of what the strategy would do now; never commands the battery."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime

from .const import MAX_BATTERY_POWER_W
from .dispatch import DispatchPlan, DispatchSettings, plan_battery_dispatch
from .net_charge import (
    ExportSettings,
    NetChargeSettings,
    should_export_discharge,
    should_net_charge,
)
from .utils import calculate_phase_power_limits


@dataclass(frozen=True)
class ShadowSettings:
    """Strategy inputs for a shadow proposal."""

    dispatch: DispatchSettings
    net_charge: NetChargeSettings
    export: ExportSettings
    capacity_kwh: float


def infer_slot_hours(starts: list[datetime]) -> float:
    """Return the price slot length in hours from forecast spacing, defaulting to a quarter."""
    gaps = sorted(
        (later - earlier).total_seconds()
        for earlier, later in zip(starts, starts[1:], strict=False)
    )
    if not gaps:
        return 0.25
    median_seconds = gaps[len(gaps) // 2]
    return min(max(median_seconds, 900.0), 3600.0) / 3600


def propose_shadow_action(
    *,
    phase_imports_w: tuple[float | None, float | None, float | None],
    battery_power_w: float | None,
    soc_pct: float | None,
    price: float | None,
    pv_power_w: float | None,
    upcoming_prices: list[float],
    slot_hours: float,
    fuse_limit_amps: float,
    phase_headroom_w: float,
    settings: ShadowSettings,
) -> DispatchPlan:
    """Return the steady-state action the strategy would take (battery power: discharge positive).

    The measured grid power already contains the battery's effect, so the load without
    the battery is reconstructed first and the proposal is what a settled zero-on-the-meter
    loop would command.
    """
    values = (*phase_imports_w, battery_power_w, soc_pct, price, pv_power_w)
    if any(value is None or not math.isfinite(value) for value in values):
        return DispatchPlan("idle", 0.0, "input_unavailable")

    assert battery_power_w is not None
    assert soc_pct is not None
    assert price is not None
    base = tuple(phase + battery_power_w / 3 for phase in phase_imports_w if phase is not None)
    max_charge, max_discharge = calculate_phase_power_limits(
        base,  # type: ignore[arg-type]
        fuse_limit_amps,
        phase_headroom_w,
        MAX_BATTERY_POWER_W,
    )
    if max_charge is None or max_discharge is None:
        return DispatchPlan("idle", 0.0, "input_unavailable")

    plan = plan_battery_dispatch(
        phase_imports_w=base,  # type: ignore[arg-type]
        battery_soc_pct=soc_pct,
        electricity_price=price,
        pv_power_w=pv_power_w,
        max_charge_power_w=max_charge,
        max_discharge_power_w=max_discharge,
        settings=settings.dispatch,
    )

    horizon = [price, *upcoming_prices]
    if (
        plan.mode != "charge"
        and max_discharge > 0
        and should_export_discharge(
            prices_eur_kwh=horizon, soc_pct=soc_pct, settings=settings.export
        )
    ):
        return DispatchPlan(
            "discharge", min(settings.export.power_w, max_discharge), "export_discharge"
        )

    if plan.mode == "idle" and soc_pct < settings.dispatch.max_soc_pct and max_charge > 0:
        headroom_kwh = (100 - soc_pct) / 100 * settings.capacity_kwh
        if should_net_charge(
            prices_eur_kwh=horizon,
            headroom_kwh=headroom_kwh,
            settings=replace(settings.net_charge, slot_hours=slot_hours),
        ):
            return DispatchPlan(
                "charge", min(settings.net_charge.charge_power_w, max_charge), "net_charge"
            )

    return plan
