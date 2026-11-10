"""Planning of grid charging power (pure, no Home Assistant calls)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .const import ONE_WAY_EFFICIENCY


def inverter_efficiency(power_w: float, *, pv_share: float = 0.0) -> float:
    """Return the assumed one-way efficiency for grid or solar power."""
    return ONE_WAY_EFFICIENCY


@dataclass(frozen=True)
class ChargePlan:
    """Whether to charge from the grid in the current slot and at what power."""

    charge: bool
    power_w: float
    slots: int
    round_trip: float


NO_CHARGE = ChargePlan(False, 0.0, 0, 0.0)


def plan_net_charge(
    *,
    prices_eur_kwh: list[float],
    headroom_kwh: float,
    slot_hours: float,
    min_earnings_eur_kwh: float,
    max_power_w: float,
    discharge_power_w: float,
    min_power_w: float,
    first_slot_remaining: float = 1.0,
    export_tax_eur_kwh: float | None = None,
) -> ChargePlan:
    """Plan grid charging over the cheap slots that gives the largest total profit.

    `prices_eur_kwh` starts with the current slot. The energy to store is spread over the
    `m` cheapest slots, each paired with one of the dearest later slots, and every pair
    must still earn `min_earnings_eur_kwh` after conversion losses. Stored energy is sold
    the better way: to the house at `discharge_power_w`, or, when `export_tax_eur_kwh` is
    given, to the grid at a power that spreads it over the dear slots. The most profitable
    number of slots wins, possibly filling only part of the headroom.
    """
    if (
        not prices_eur_kwh
        or not all(math.isfinite(price) for price in prices_eur_kwh)
        or not math.isfinite(headroom_kwh)
        or headroom_kwh <= 0
        or max_power_w <= 0
        or slot_hours <= 0
        or discharge_power_w <= 0
        or len(prices_eur_kwh) < 2
    ):
        return NO_CHARGE

    frac = min(max(first_slot_remaining, 0.05), 1.0)
    cheapest = sorted(prices_eur_kwh)
    dearest = sorted(prices_eur_kwh[1:], reverse=True)
    house_eff = inverter_efficiency(discharge_power_w)
    low_power = min(min_power_w, max_power_w)

    def sale_value(dear: float, share_kwh: float) -> tuple[float, float]:
        """Return (EUR per stored kWh, discharge efficiency) for the better sales route."""
        best = (dear * house_eff, house_eff)
        if export_tax_eur_kwh is not None:
            power = min(max(share_kwh / slot_hours * 1000, low_power), max_power_w)
            export_eff = inverter_efficiency(power)
            export = ((dear - export_tax_eur_kwh) * export_eff, export_eff)
            best = max(best, export)
        return best

    best_plan: tuple[float, int, float, float] | None = None  # profit, slots, power, round trip
    for slots in range(min(len(cheapest), len(dearest)), 0, -1):
        # The unfinished current slot only counts partially.
        hours = (slots - 1 + frac) * slot_hours
        full = headroom_kwh / ONE_WAY_EFFICIENCY / hours * 1000
        power = min(max(full, low_power), max_power_w)
        charge_eff = inverter_efficiency(power)
        stored = min(headroom_kwh, power / 1000 * hours * charge_eff)
        if stored <= 0:
            continue
        share = stored / slots
        sales = [sale_value(dear, share) for dear in dearest[:slots]]
        pairs = list(zip(cheapest[:slots], sales, strict=False))
        if any(value * charge_eff - cheap < min_earnings_eur_kwh for cheap, (value, _) in pairs):
            continue
        profit = sum(share * (value - cheap / charge_eff) for cheap, (value, _) in pairs)
        if best_plan is None or profit > best_plan[0] + 1e-9:
            round_trip = charge_eff * sum(eff for _, (_, eff) in pairs) / slots
            best_plan = (profit, slots, power, round_trip)

    if best_plan is None:
        return NO_CHARGE
    _, slots, power, round_trip = best_plan
    return ChargePlan(prices_eur_kwh[0] <= cheapest[slots - 1], power, slots, round_trip)
