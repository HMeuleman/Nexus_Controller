"""Efficiency-aware planning of grid charging power (pure, no Home Assistant calls)."""

from __future__ import annotations

import math
from dataclasses import dataclass

# Inverter efficiency estimates: 95 % at 500 W falling to 85 % at 10 kW.
LOW_POINT_W = 500.0
LOW_EFFICIENCY = 0.95
HIGH_POINT_W = 10_000.0
HIGH_EFFICIENCY = 0.85


def inverter_efficiency(power_w: float) -> float:
    """Return the one-way conversion efficiency at a power level (linear between estimates)."""
    power = min(max(abs(power_w), LOW_POINT_W), HIGH_POINT_W)
    fraction = (power - LOW_POINT_W) / (HIGH_POINT_W - LOW_POINT_W)
    return LOW_EFFICIENCY + fraction * (HIGH_EFFICIENCY - LOW_EFFICIENCY)


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
    """Plan grid charging as slowly as the cheap slots allow, because slow is more efficient.

    `prices_eur_kwh` starts with the current slot. The energy to store is spread over the
    most cheap slots that all still earn `min_earnings_eur_kwh` after conversion losses.
    Without enough profitable slots it charges at full power during the profitable ones.
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

    def profitable(slots: int, round_trip: float) -> bool:
        return all(
            dear * round_trip - cheap >= min_earnings_eur_kwh
            for cheap, dear in zip(cheapest[:slots], dearest, strict=False)
        )

    for slots in range(min(len(prices_eur_kwh), len(cheapest)), 0, -1):
        power = power_for(slots)
        if power > max_power_w or power < min_power_w:
            continue
        round_trip = inverter_efficiency(power) * discharge_eff
        if slots <= len(dearest) and profitable(slots, round_trip):
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
