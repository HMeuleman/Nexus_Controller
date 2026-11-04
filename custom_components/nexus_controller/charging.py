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
) -> ChargePlan:
    """Plan grid charging over the cheap slots that gives the largest total profit.

    `prices_eur_kwh` starts with the current slot. The energy to store is spread over the
    `m` cheapest slots, each paired with one of the dearest later slots, and every pair
    must still earn `min_earnings_eur_kwh` after conversion losses. Efficiency peaks at a
    mid power, so the best `m` is the one with the highest profit, not simply the lowest
    power. Without a fully profitable spread it charges at full power in the profitable
    slots only.
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
    discharge_eff = inverter_efficiency(discharge_power_w)

    def power_for(slots: int) -> float:
        # Slot time counts the unfinished current slot only partially.
        hours = (slots - 1 + frac) * slot_hours
        power = headroom_kwh / hours * 1000
        for _ in range(3):
            power = headroom_kwh / inverter_efficiency(power) / hours * 1000
        return power

    best: tuple[float, int, float, float] | None = None  # profit, slots, power, round trip
    for slots in range(min(len(cheapest), len(dearest)), 0, -1):
        power = power_for(slots)
        if power > max_power_w or power < min_power_w:
            continue
        charge_eff = inverter_efficiency(power)
        pairs = list(zip(cheapest[:slots], dearest, strict=False))
        if any(
            dear * charge_eff * discharge_eff - cheap < min_earnings_eur_kwh
            for cheap, dear in pairs
        ):
            continue
        share = headroom_kwh / slots
        profit = sum(share * (dear * discharge_eff - cheap / charge_eff) for cheap, dear in pairs)
        if best is None or profit > best[0] + 1e-9:
            best = (profit, slots, power, charge_eff * discharge_eff)
    if best is not None:
        _, slots, power, round_trip = best
        return _plan(prices_eur_kwh[0], cheapest, slots, power, round_trip)

    # Not enough profitable slots for a full charge: use the profitable ones at full power.
    round_trip = inverter_efficiency(max_power_w) * discharge_eff
    count = 0
    for cheap, dear in zip(cheapest, dearest, strict=False):
        if dear * round_trip - cheap < min_earnings_eur_kwh:
            break
        count += 1
    if count == 0:
        return NO_CHARGE
    return _plan(prices_eur_kwh[0], cheapest, count, max_power_w, round_trip)


def _plan(current: float, cheapest: list[float], slots: int, power: float, rt: float) -> ChargePlan:
    """Charge now only when the current slot is one of the chosen cheapest slots."""
    return ChargePlan(current <= cheapest[slots - 1], power, slots, rt)
