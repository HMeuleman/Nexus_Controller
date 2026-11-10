"""Offline quarter-hour arbitrage prototype; never controls the battery."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache

from .charging import inverter_efficiency
from .const import MAX_BATTERY_POWER_W
from .dispatch import DispatchPlan

MAX_PLANNER_WORK = 20_000_000


@dataclass(frozen=True)
class QuarterPlan:
    """AC power is positive for discharge and negative for charging."""

    power_w: tuple[float, ...]
    soc_pct: tuple[float, ...]
    profit_eur: float
    charge_source: tuple[str, ...] = ()


def current_plan_dispatch(
    slot: dict | None,
    *,
    now: datetime,
    soc_pct: float | None,
    min_soc_pct: float,
    export_reserve_soc_pct: float,
    max_charge_w: float | None,
    max_discharge_w: float | None,
    grid_import_w: float | None,
    battery_power_w: float | None,
    pv_power_w: float | None = None,
    measurement_ages_s: tuple[float, ...] | None = None,
    max_measurement_age_s: float | None = None,
) -> DispatchPlan:
    """Validate one plan slot against current measurements without issuing writes."""
    idle = DispatchPlan("idle", 0.0, "planned_inputs_unavailable")
    if (
        not isinstance(slot, dict)
        or now.tzinfo is None
        or measurement_ages_s is None
        or len(measurement_ages_s) != 5
        or max_measurement_age_s is None
        or not math.isfinite(max_measurement_age_s)
        or max_measurement_age_s <= 0
        or any(
            not math.isfinite(age) or age < 0 or age > max_measurement_age_s
            for age in measurement_ages_s
        )
    ):
        return idle
    try:
        start = datetime.fromisoformat(slot["start"].replace("Z", "+00:00"))
        duration = float(slot["duration_hours"])
        power = float(slot["power_w"])
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return idle
    values = (
        duration,
        power,
        soc_pct,
        min_soc_pct,
        export_reserve_soc_pct,
        max_charge_w,
        max_discharge_w,
        grid_import_w,
        battery_power_w,
    )
    if any(value is None or not math.isfinite(value) for value in values):
        return idle
    assert soc_pct is not None
    assert max_charge_w is not None
    assert max_discharge_w is not None
    assert grid_import_w is not None
    assert battery_power_w is not None
    if (
        start.tzinfo is None
        or not 0 < duration <= 0.25
        or not 0 <= min_soc_pct <= export_reserve_soc_pct <= 100
        or not 0 <= soc_pct <= 100
        or max_charge_w < 0
        or max_discharge_w < 0
        or not start.timestamp()
        <= now.timestamp()
        < (start + timedelta(hours=duration)).timestamp()
    ):
        return idle
    if power > 0 and max_discharge_w > 0:
        discharge_mode = slot.get("discharge_mode")
        if discharge_mode == "nom" and soc_pct > min_soc_pct:
            demand = grid_import_w + battery_power_w
            if demand > 0:
                return DispatchPlan(
                    "discharge",
                    min(power, max_discharge_w, demand, MAX_BATTERY_POWER_W),
                    "high_price_no_solar_reduce_import",
                )
        if discharge_mode == "export" and soc_pct > export_reserve_soc_pct:
            return DispatchPlan(
                "discharge", min(power, max_discharge_w, MAX_BATTERY_POWER_W), "planned_export"
            )
    if power < 0 and soc_pct < 100 and max_charge_w > 0:
        source = slot.get("charge_source")
        if source == "solar":
            if pv_power_w is None or not math.isfinite(pv_power_w) or pv_power_w <= 0:
                return DispatchPlan("idle", 0.0, "planned_solar_production_unavailable")
            surplus = -(grid_import_w + battery_power_w)
            if surplus <= 0:
                return DispatchPlan("idle", 0.0, "planned_solar_surplus_unavailable")
            return DispatchPlan(
                "charge",
                min(-power, max_charge_w, surplus, pv_power_w, MAX_BATTERY_POWER_W),
                "pv_surplus_reduce_export",
            )
        if source == "grid":
            return DispatchPlan(
                "charge", min(-power, max_charge_w, MAX_BATTERY_POWER_W), "planned_grid_charge"
            )
    return DispatchPlan("idle", 0.0, "planned_idle_or_limit")


def forecast_quarters(
    forecast: list[tuple[datetime, float]], now: datetime
) -> list[tuple[datetime, float]]:
    """Select full future quarters, rejecting gaps and unsupported resolutions."""
    future = [(start, price) for start, price in forecast if start > now]
    if len(future) < 2 or len(future) > 200:
        return []
    if any(
        later[0].timestamp() - earlier[0].timestamp() != 900
        for earlier, later in zip(future, future[1:], strict=False)
    ):
        return []
    if (future[0][0] - now).total_seconds() > 900:
        return []
    return future


def forecast_quarters_with_current(
    forecast: list[tuple[datetime, float]], now: datetime
) -> list[tuple[datetime, float, float]]:
    """Return the active price interval and future quarters with their remaining duration."""
    future = [(start, price) for start, price in forecast if start > now]
    if not future or len(future) > 200:
        return []
    if any(
        later[0].timestamp() - earlier[0].timestamp() != 900
        for earlier, later in zip(future, future[1:], strict=False)
    ):
        return []
    if future[0][0].timestamp() - now.timestamp() > 900:
        return []
    previous = [(start, price) for start, price in forecast if start <= now]
    if not previous:
        if len(future) < 2:
            return []
        return [(start, price, 0.25) for start, price in future]
    start, price = previous[-1]
    remaining = 900 - (now.timestamp() - start.timestamp())
    if remaining <= 0 or abs(future[0][0].timestamp() - start.timestamp() - 900) > 1e-6:
        return []
    return [(start, price, remaining / 3600)] + [
        (future_start, future_price, 0.25) for future_start, future_price in future[:199]
    ]


def plan_quarters(
    prices: list[float],
    *,
    capacity_kwh: float,
    initial_soc_pct: float,
    min_soc_pct: float,
    max_power_w: float,
    efficiency: float,
    min_profit_eur_per_kwh: float,
    export_tax_eur_per_kwh: float,
    house_load_w: float = 0.0,
    initial_value_eur_per_kwh: float | None = None,
    export_taxes_eur_per_kwh: list[float] | None = None,
    solar_kwh: list[float] | None = None,
    durations_hours: list[float] | None = None,
    use_power_efficiency: bool = False,
    export_reserve_soc_pct: float | None = None,
) -> QuarterPlan:
    """Optimize complete, nonoverlapping grid cycles with estimated efficiency.

    Each cycle has contiguous charge and discharge blocks and returns to the
    starting SOC. Existing inventory can be discharged across a bounded set of levels
    between subsequent cycles. Solar surplus is valued at the forgone export price.
    """
    numbers = (
        *prices,
        capacity_kwh,
        initial_soc_pct,
        min_soc_pct,
        max_power_w,
        efficiency,
        min_profit_eur_per_kwh,
        export_tax_eur_per_kwh,
        house_load_w,
        *(value for value in (initial_value_eur_per_kwh,) if value is not None),
        *(export_taxes_eur_per_kwh or ()),
        *(solar_kwh or ()),
        *(durations_hours or ()),
        *(value for value in (export_reserve_soc_pct,) if value is not None),
    )
    if (
        not all(math.isfinite(value) for value in numbers)
        or capacity_kwh <= 0
        or not 0 <= min_soc_pct <= initial_soc_pct <= 100
        or max_power_w <= 0
        or not 0 < efficiency <= 1
        or min_profit_eur_per_kwh < 0
        or export_tax_eur_per_kwh < 0
        or house_load_w < 0
        or len(prices) > 200
        or (export_reserve_soc_pct is not None and not min_soc_pct <= export_reserve_soc_pct <= 100)
        or (
            solar_kwh is not None
            and (len(solar_kwh) != len(prices) or any(v < 0 for v in solar_kwh))
        )
        or (
            durations_hours is not None
            and (
                len(durations_hours) != len(prices)
                or any(not 0 < duration <= 0.25 for duration in durations_hours)
            )
        )
        or (
            export_taxes_eur_per_kwh is not None
            and (
                len(export_taxes_eur_per_kwh) != len(prices)
                or any(tax < 0 for tax in export_taxes_eur_per_kwh)
            )
        )
    ):
        raise ValueError("Invalid quarter planner inputs")

    count = len(prices)
    input_per_slot = max_power_w / 4000
    if not count:
        return QuarterPlan((0.0,) * count, (initial_soc_pct,) * count, 0.0)
    available = capacity_kwh * (initial_soc_pct - min_soc_pct) / 100
    max_discharge_efficiency = (
        inverter_efficiency(max_power_w) if use_power_efficiency else efficiency
    )
    efficiency_floor = 0.85 if use_power_efficiency else efficiency
    inventory_step = max(available / 10, input_per_slot / max_discharge_efficiency)
    levels = [0.0]
    if initial_value_eur_per_kwh is not None and available > 0:
        levels.extend(
            min(available, inventory_step * index)
            for index in range(1, math.ceil(available / inventory_step) + 1)
        )
        if durations_hours is not None and durations_hours[0] < 0.25:
            partial_level = min(
                available, max_power_w * durations_hours[0] / 1000 / max_discharge_efficiency
            )
            levels = sorted(set([*levels, partial_level]))
    initial_headroom = capacity_kwh * (100 - initial_soc_pct) / 100
    headrooms = [initial_headroom + level for level in levels]
    estimated_work = (
        count
        * (count + 1)
        // 2
        * (
            sum(
                min(count, math.ceil(headroom / (input_per_slot * efficiency_floor))) ** 2
                for headroom in headrooms
            )
            + len(levels) ** 2
        )
    )
    if estimated_work > MAX_PLANNER_WORK:
        raise ValueError("Quarter planner exceeds work limit")
    prefix = [0.0]
    hours = durations_hours or [0.25] * count
    hour_prefix = [0.0]
    for price, duration in zip(prices, hours, strict=True):
        hour_prefix.append(hour_prefix[-1] + duration)
        prefix.append(prefix[-1] + price * duration)
    tax_prefix = [0.0]
    taxes = export_taxes_eur_per_kwh or [export_tax_eur_per_kwh] * count
    for tax, duration in zip(taxes, hours, strict=True):
        tax_prefix.append(tax_prefix[-1] + tax * duration)
    solar_prefix = [0.0]
    solar_cost_prefix = [0.0]
    solar_cell_prefix = [0.0]
    charge_cell_prefix = [0.0]
    solar_surplus = []
    for index in range(count):
        energy = (
            min(
                max(0.0, solar_kwh[index] - house_load_w * hours[index] / 1000),
                max_power_w * hours[index] / 1000,
            )
            if solar_kwh is not None
            else 0.0
        )
        solar_surplus.append(energy)
        solar_prefix.append(solar_prefix[-1] + energy)
        solar_cost_prefix.append(solar_cost_prefix[-1] + energy * (prices[index] - taxes[index]))
        solar_power = energy * 1000 / hours[index]
        solar_efficiency = (
            inverter_efficiency(solar_power, pv_share=1.0) if use_power_efficiency else efficiency
        )
        solar_cell_prefix.append(solar_cell_prefix[-1] + energy * solar_efficiency)
        input_energy = max_power_w * hours[index] / 1000
        charge_efficiency = (
            inverter_efficiency(max_power_w, pv_share=min(1.0, energy / input_energy))
            if use_power_efficiency
            else efficiency
        )
        charge_cell_prefix.append(charge_cell_prefix[-1] + input_energy * charge_efficiency)

    def charge_cell_energy(start: int, end: int, power_w: float) -> float:
        if not use_power_efficiency:
            return power_w * (hour_prefix[end] - hour_prefix[start]) / 1000 * efficiency
        if solar_kwh is None:
            return (
                power_w
                * (hour_prefix[end] - hour_prefix[start])
                / 1000
                * inverter_efficiency(power_w)
            )
        return sum(
            power_w
            * hours[index]
            / 1000
            * inverter_efficiency(
                power_w,
                pv_share=min(1.0, solar_surplus[index] / (power_w * hours[index] / 1000)),
            )
            for index in range(start, end)
        )

    def discharge_cell_energy(start: int, end: int, power_w: float) -> float:
        effective = inverter_efficiency(power_w) if use_power_efficiency else efficiency
        return power_w * (hour_prefix[end] - hour_prefix[start]) / 1000 / effective

    def solar_cell_energy(start: int, end: int, fraction: float) -> float:
        if not use_power_efficiency:
            return (solar_prefix[end] - solar_prefix[start]) * fraction * efficiency
        return sum(
            solar_surplus[index]
            * fraction
            * inverter_efficiency(
                solar_surplus[index] * fraction * 1000 / hours[index], pv_share=1.0
            )
            for index in range(start, end)
        )

    def solve_level(target: float, maximum: float, maximum_energy: float, energy_at) -> float:
        if target >= maximum_energy - 1e-12:
            return maximum
        low, high = 0.0, maximum
        candidate = maximum * target / maximum_energy
        for _ in range(40):
            actual = energy_at(candidate)
            if abs(actual - target) <= max(1e-12, abs(target) * 1e-10):
                return candidate
            if actual < target:
                low = candidate
            else:
                high = candidate
            estimate = candidate * target / actual if actual > 0 else (low + high) / 2
            candidate = estimate if low < estimate < high else (low + high) / 2
        return (low + high) / 2

    @lru_cache(maxsize=16_384)
    def charge_power(start: int, end: int, stored: float) -> float:
        if not use_power_efficiency:
            return stored / efficiency * 1000 / (hour_prefix[end] - hour_prefix[start])
        return solve_level(
            stored,
            max_power_w,
            charge_cell_prefix[end] - charge_cell_prefix[start],
            lambda power: charge_cell_energy(start, end, power),
        )

    @lru_cache(maxsize=16_384)
    def discharge_power(start: int, end: int, stored: float) -> float:
        if not use_power_efficiency:
            return stored * efficiency * 1000 / (hour_prefix[end] - hour_prefix[start])
        maximum_energy = (
            max_power_w * (hour_prefix[end] - hour_prefix[start]) / 1000 / max_discharge_efficiency
        )
        return solve_level(
            stored,
            max_power_w,
            maximum_energy,
            lambda power: discharge_cell_energy(start, end, power),
        )

    @lru_cache(maxsize=16_384)
    def solar_fraction(start: int, end: int, stored: float) -> float:
        if not use_power_efficiency:
            return stored / efficiency / (solar_prefix[end] - solar_prefix[start])
        return solve_level(
            stored,
            1.0,
            solar_cell_prefix[end] - solar_cell_prefix[start],
            lambda fraction: solar_cell_energy(start, end, fraction),
        )

    @lru_cache(maxsize=16_384)
    def sale_limit(start: int, end: int, state: int) -> float:
        baseline_soc = initial_soc_pct - levels[state] / capacity_kwh * 100
        if export_reserve_soc_pct is None or baseline_soc + 1e-9 >= export_reserve_soc_pct:
            return max_power_w
        return min(
            max_power_w,
            *(
                max(
                    0.0,
                    house_load_w
                    - ((solar_kwh[index] * 1000 / hours[index]) if solar_kwh is not None else 0.0),
                )
                for index in range(start, end)
            ),
        )

    @lru_cache(maxsize=16_384)
    def sale_price(start: int, end: int, discharge_w: float) -> float:
        block_hours = hour_prefix[end] - hour_prefix[start]
        if solar_kwh is None:
            export_share = max(0.0, 1 - house_load_w / discharge_w)
            return (
                prefix[end] - prefix[start] - (tax_prefix[end] - tax_prefix[start]) * export_share
            ) / block_hours
        return (
            sum(
                hours[index]
                * (
                    prices[index]
                    - taxes[index]
                    * max(
                        0.0,
                        1
                        - max(0.0, house_load_w * hours[index] / 1000 - solar_kwh[index])
                        / (discharge_w * hours[index] / 1000),
                    )
                )
                for index in range(start, end)
            )
            / block_hours
        )

    @lru_cache(maxsize=16_384)
    def purchase_price(start: int, end: int, charge_w: float) -> float:
        block_hours = hour_prefix[end] - hour_prefix[start]
        if solar_kwh is None:
            return (prefix[end] - prefix[start]) / block_hours
        return (
            sum(
                hours[index]
                * (
                    prices[index]
                    - taxes[index]
                    * min(
                        1.0,
                        max(0.0, solar_kwh[index] - house_load_w * hours[index] / 1000)
                        / (charge_w * hours[index] / 1000),
                    )
                )
                for index in range(start, end)
            )
            / block_hours
        )

    best = [[0.0] * (count + 1)] + [[-math.inf] * (count + 1) for _ in levels[1:]]
    choices: list[list[tuple | None]] = [[None] * (count + 1) for _ in levels]
    for end in range(1, count + 1):
        for state, headroom in enumerate(headrooms):
            best[state][end] = best[state][end - 1]
            if headroom <= 0:
                continue
            max_blocks = min(
                count,
                math.ceil(headroom / (input_per_slot * efficiency_floor))
                + int(durations_hours is not None and any(duration < 0.25 for duration in hours)),
            )
            for sell_count in range(1, min(max_blocks, end - 1) + 1):
                sell_start = end - sell_count
                max_sale_power = sale_limit(sell_start, end, state)
                if max_sale_power <= 0:
                    continue
                for buy_end in range(1, sell_start + 1):
                    for buy_count in range(1, min(max_blocks, buy_end) + 1):
                        buy_start = buy_end - buy_count
                        stored = min(
                            headroom,
                            charge_cell_prefix[buy_end] - charge_cell_prefix[buy_start],
                            discharge_cell_energy(sell_start, end, max_sale_power),
                        )
                        discharge_w = discharge_power(sell_start, end, stored)
                        charge_w = charge_power(buy_start, buy_end, stored)
                        sold = discharge_w * (hour_prefix[end] - hour_prefix[sell_start]) / 1000
                        bought = charge_w * (hour_prefix[buy_end] - hour_prefix[buy_start]) / 1000
                        gain = (
                            sale_price(sell_start, end, discharge_w) * sold
                            - purchase_price(buy_start, buy_end, charge_w) * bought
                        )
                        margin = gain / sold
                        if margin + 1e-12 >= min_profit_eur_per_kwh:
                            profit = best[state][buy_start] + gain
                            if profit > best[state][end] + 1e-12:
                                best[state][end] = profit
                                choices[state][end] = (
                                    "cycle",
                                    buy_start,
                                    buy_end,
                                    sell_start,
                                    stored,
                                    charge_w,
                                    discharge_w,
                                )
                        solar_energy = solar_prefix[buy_end] - solar_prefix[buy_start]
                        if solar_energy <= 0:
                            continue
                        solar_stored = min(
                            headroom,
                            solar_cell_prefix[buy_end] - solar_cell_prefix[buy_start],
                            discharge_cell_energy(sell_start, end, max_sale_power),
                        )
                        solar_discharge = discharge_power(sell_start, end, solar_stored)
                        fraction = solar_fraction(buy_start, buy_end, solar_stored)
                        solar_sold = (
                            solar_discharge * (hour_prefix[end] - hour_prefix[sell_start]) / 1000
                        )
                        solar_purchase = (
                            solar_cost_prefix[buy_end] - solar_cost_prefix[buy_start]
                        ) / solar_energy
                        solar_gain = (
                            sale_price(sell_start, end, solar_discharge) * solar_sold
                            - solar_purchase * solar_energy * fraction
                        )
                        solar_margin = solar_gain / solar_sold
                        if solar_margin + 1e-12 < min_profit_eur_per_kwh:
                            continue
                        solar_profit = best[state][buy_start] + solar_gain
                        if solar_profit > best[state][end] + 1e-12:
                            best[state][end] = solar_profit
                            choices[state][end] = (
                                "solar_cycle",
                                buy_start,
                                buy_end,
                                sell_start,
                                solar_stored,
                                fraction,
                                solar_discharge,
                            )

        if initial_value_eur_per_kwh is not None:
            for state, level in enumerate(levels[1:], start=1):
                for previous_state in range(state):
                    stored = level - levels[previous_state]
                    min_slots = math.ceil(
                        stored * max_discharge_efficiency / input_per_slot - 1e-12
                    )
                    for sell_count in range(min_slots, end + 1):
                        sell_start = end - sell_count
                        max_sale_power = sale_limit(sell_start, end, state)
                        if (
                            max_sale_power <= 0
                            or discharge_cell_energy(sell_start, end, max_sale_power) + 1e-9
                            < stored
                        ):
                            continue
                        discharge_w = discharge_power(sell_start, end, stored)
                        sold = discharge_w * (hour_prefix[end] - hour_prefix[sell_start]) / 1000
                        gain = (
                            sale_price(sell_start, end, discharge_w) * sold
                            - stored * initial_value_eur_per_kwh
                        )
                        margin = gain / sold
                        if margin + 1e-12 < min_profit_eur_per_kwh:
                            continue
                        profit = best[previous_state][sell_start] + gain
                        if profit > best[state][end] + 1e-12:
                            best[state][end] = profit
                            choices[state][end] = (
                                "initial",
                                sell_start,
                                stored,
                                previous_state,
                                discharge_w,
                            )

    powers = [0.0] * count
    charge_sources = ["none"] * count
    cursor = count
    state = max(range(len(levels)), key=lambda level: best[level][count])
    profit = best[state][count]
    while cursor:
        choice = choices[state][cursor]
        if choice is None:
            cursor -= 1
            continue
        if choice[0] == "initial":
            _, sell_start, stored, previous_state, discharge_w = choice
            for index in range(sell_start, cursor):
                powers[index] = discharge_w
            cursor = sell_start
            state = previous_state
            continue
        _, buy_start, buy_end, sell_start, stored, charge_parameter, discharge_w = choice
        if choice[0] == "solar_cycle":
            fraction = charge_parameter
            for index in range(buy_start, buy_end):
                powers[index] = -solar_surplus[index] * fraction * 1000 / hours[index]
                if powers[index] < 0:
                    charge_sources[index] = "solar"
        else:
            charge_w = charge_parameter
            for index in range(buy_start, buy_end):
                powers[index] = -charge_w
                charge_sources[index] = "grid"
        for index in range(sell_start, cursor):
            powers[index] = discharge_w
        cursor = buy_start

    stored_kwh = capacity_kwh * initial_soc_pct / 100
    soc = []
    for index, (power, duration) in enumerate(zip(powers, hours, strict=True)):
        if power < 0:
            if use_power_efficiency:
                energy = -power * duration / 1000
                pv_share = min(1.0, solar_surplus[index] / energy)
                effective = inverter_efficiency(power, pv_share=pv_share)
            else:
                effective = efficiency
            stored_kwh += -power * effective * duration / 1000
        elif power > 0:
            effective = inverter_efficiency(power) if use_power_efficiency else efficiency
            stored_kwh -= power / effective * duration / 1000
        value = stored_kwh / capacity_kwh * 100
        if abs(value - min_soc_pct) < 1e-8:
            value = min_soc_pct
        elif abs(value - 100) < 1e-8:
            value = 100.0
        soc.append(value)
    return QuarterPlan(tuple(powers), tuple(soc), profit, tuple(charge_sources))
