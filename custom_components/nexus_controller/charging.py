"""Efficiency-aware planning of grid charging power (pure, no Home Assistant calls)."""

from __future__ import annotations

import math
from dataclasses import dataclass

# One-way conversion efficiency by power: (watts, charging from grid, charging from PV).
# Midpoints of the measured ranges; the grid column also stands in for discharging.
EFFICIENCY_TABLE = (
    (250.0, 0.850, 0.880),
    (500.0, 0.875, 0.900),
    (1_000.0, 0.900, 0.920),
    (2_000.0, 0.935, 0.955),
    (5_000.0, 0.975, 0.984),
    (7_500.0, 0.975, 0.984),
    (10_000.0, 0.9575, 0.9675),
)
BEST_POWER_W = 7_500.0


def inverter_efficiency(power_w: float, *, pv_share: float = 0.0) -> float:
    """Return the one-way efficiency at a power level, blending grid and PV charging."""
    power = abs(power_w)
    share = min(max(pv_share, 0.0), 1.0)
    if power <= EFFICIENCY_TABLE[0][0]:
        return EFFICIENCY_TABLE[0][1] * (1 - share) + EFFICIENCY_TABLE[0][2] * share
    for (low_w, low_grid, low_pv), (high_w, high_grid, high_pv) in zip(
        EFFICIENCY_TABLE, EFFICIENCY_TABLE[1:], strict=False
    ):
        if power <= high_w:
            fraction = (power - low_w) / (high_w - low_w)
            grid = low_grid + fraction * (high_grid - low_grid)
            pv = low_pv + fraction * (high_pv - low_pv)
            return grid * (1 - share) + pv * share
    return EFFICIENCY_TABLE[-1][1] * (1 - share) + EFFICIENCY_TABLE[-1][2] * share


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
    given, to the grid at a power that spreads it over the dear slots. Efficiency peaks at
    a mid power, so the most profitable (slots, power) wins, possibly filling only part of
    the headroom.
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

    def fill_power(slots: int, hours: float) -> float:
        power = headroom_kwh / hours * 1000
        for _ in range(3):
            power = headroom_kwh / inverter_efficiency(power) / hours * 1000
        return power

    best_plan: tuple[float, int, float, float] | None = None  # profit, slots, power, round trip
    for slots in range(min(len(cheapest), len(dearest)), 0, -1):
        # The unfinished current slot only counts partially.
        hours = (slots - 1 + frac) * slot_hours
        full = fill_power(slots, hours)
        candidates = {min(max(full, low_power), max_power_w)}
        if full > BEST_POWER_W:
            candidates.add(min(BEST_POWER_W, max_power_w))
        for power in candidates:
            charge_eff = inverter_efficiency(power)
            stored = min(headroom_kwh, power / 1000 * hours * charge_eff)
            if stored <= 0:
                continue
            share = stored / slots
            sales = [sale_value(dear, share) for dear in dearest[:slots]]
            pairs = list(zip(cheapest[:slots], sales, strict=False))
            if any(
                value * charge_eff - cheap < min_earnings_eur_kwh for cheap, (value, _) in pairs
            ):
                continue
            profit = sum(share * (value - cheap / charge_eff) for cheap, (value, _) in pairs)
            if best_plan is None or profit > best_plan[0] + 1e-9:
                round_trip = charge_eff * sum(eff for _, (_, eff) in pairs) / slots
                best_plan = (profit, slots, power, round_trip)

    if best_plan is None:
        return NO_CHARGE
    _, slots, power, round_trip = best_plan
    return ChargePlan(prices_eur_kwh[0] <= cheapest[slots - 1], power, slots, round_trip)
