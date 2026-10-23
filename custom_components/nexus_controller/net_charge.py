"""Offline-only price-based grid charging plan (separate from the PID loop)."""

from __future__ import annotations

import math
from dataclasses import dataclass

QUARTER_HOURS = 0.25


@dataclass(frozen=True)
class NetChargeSettings:
    """Economics for charging from the grid and discharging later."""

    min_margin_eur_kwh: float
    round_trip_efficiency: float
    charge_power_w: float


def should_net_charge(
    *,
    prices_eur_kwh: list[float],
    headroom_kwh: float,
    settings: NetChargeSettings,
) -> bool:
    """Decide whether the first quarter in `prices_eur_kwh` is worth charging in.

    The list starts with the current quarter and runs to the end of the known
    horizon. The cheapest quarters needed to fill the headroom are paired with
    the dearest later quarters; the current quarter qualifies when its own pair
    clears the margin after round-trip losses. Discharge sizing against the
    actual house load is not modelled.
    """
    if (
        not prices_eur_kwh
        or not all(math.isfinite(price) for price in prices_eur_kwh)
        or not math.isfinite(headroom_kwh)
        or headroom_kwh <= 0
        or settings.charge_power_w <= 0
        or not 0 < settings.round_trip_efficiency <= 1
        or settings.min_margin_eur_kwh < 0
    ):
        return False

    current_price = prices_eur_kwh[0]
    later_prices = prices_eur_kwh[1:]
    if not later_prices:
        return False

    quarters_needed = math.ceil(headroom_kwh / (settings.charge_power_w / 1000 * QUARTER_HOURS))
    cheapest = sorted(prices_eur_kwh)[:quarters_needed]
    dearest = sorted(later_prices, reverse=True)

    profitable_pairs = 0
    for cheap_price, dear_price in zip(cheapest, dearest, strict=False):
        if dear_price * settings.round_trip_efficiency - cheap_price < settings.min_margin_eur_kwh:
            break
        profitable_pairs += 1
    if profitable_pairs == 0:
        return False

    return current_price <= cheapest[profitable_pairs - 1]
