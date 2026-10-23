"""Closed-loop simulation: the planner drives a simple battery model on the recorded house load."""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace

from custom_components.nexus_controller.control import PIDMemory, PIDTuning, simulate_dispatch_cycle
from custom_components.nexus_controller.dispatch import (
    DEFAULT_DISPATCH_SETTINGS,
    DispatchPlan,
    DispatchSettings,
)
from custom_components.nexus_controller.net_charge import (
    DischargeThresholdSettings,
    ExportSettings,
    NetChargeSettings,
    discharge_price_threshold,
    should_export_discharge,
    should_net_charge,
)
from custom_components.nexus_controller.utils import calculate_phase_power_limits
from sim.replay import ReplaySample, _build_parser, load_trace_csv

RESULT_COLUMNS = (
    "timestamp",
    "base_net_w",
    "sim_net_w",
    "actual_net_w",
    "sim_battery_w",
    "actual_battery_w",
    "sim_soc_pct",
    "actual_soc_pct",
    "price_eur_kwh",
    "mode",
    "reason",
)


@dataclass(frozen=True)
class BatteryModel:
    """Idealised battery: linear ramp, constant one-way efficiency, three balanced phases."""

    capacity_kwh: float
    one_way_efficiency: float
    ramp_w_per_s: float
    max_power_w: float


def simulate_closed_loop(
    samples: list[ReplaySample],
    *,
    tuning: PIDTuning,
    battery: BatteryModel,
    settings: DispatchSettings = DEFAULT_DISPATCH_SETTINGS,
    fuse_limit_amps: float = 25,
    phase_headroom_w: float = 1000,
    max_sample_gap_seconds: float = 900,
    net_charge: NetChargeSettings | None = None,
    export: ExportSettings | None = None,
    dynamic_discharge: DischargeThresholdSettings | None = None,
    horizon_quarters: int = 48,
) -> list[dict[str, float | str]]:
    """Run the planner on house load with the recorded battery power removed."""
    if len(samples) < 2:
        raise ValueError("Simulation requires at least two samples")
    if (
        not 0 < battery.one_way_efficiency <= 1
        or battery.capacity_kwh <= 0
        or battery.ramp_w_per_s <= 0
        or battery.max_power_w <= 0
    ):
        raise ValueError("Invalid battery model")

    quarter_prices = _quarter_prices(samples)
    rows: list[dict[str, float | str]] = []
    memory = PIDMemory()
    power_w = 0.0  # signed: positive discharges, negative charges
    soc_pct = samples[0].battery_soc_pct
    if soc_pct is None:
        raise ValueError("First sample needs a battery SOC")

    for index, sample in enumerate(samples):
        if (
            None in sample.phase_imports_w
            or sample.battery_power_w is None
            or sample.battery_soc_pct is None
            or sample.pv_power_w is None
            or sample.electricity_price_eur_kwh is None
        ):
            raise ValueError("Trace rows must be complete for closed-loop simulation")

        interval = (
            (samples[1].timestamp - sample.timestamp).total_seconds()
            if index == 0
            else (sample.timestamp - samples[index - 1].timestamp).total_seconds()
        )
        if interval <= 0:
            raise ValueError("Timestamps must be strictly increasing")
        if interval > max_sample_gap_seconds:
            memory = PIDMemory()
            power_w = 0.0
            soc_pct = sample.battery_soc_pct
            interval = 0.0

        # Recorded grid minus recorded discharge (split evenly) is the load without any battery.
        base = tuple(phase + sample.battery_power_w / 3 for phase in sample.phase_imports_w)
        sim_phases = tuple(phase - power_w / 3 for phase in base)

        # Caps apply to total battery power, so they come from the load without the battery.
        max_charge, max_discharge = calculate_phase_power_limits(
            base, fuse_limit_amps, phase_headroom_w, battery.max_power_w
        )
        quarter = int(sample.timestamp.timestamp() // 900)
        horizon = [sample.electricity_price_eur_kwh] + [
            quarter_prices[q]
            for q in range(quarter + 1, quarter + horizon_quarters)
            if q in quarter_prices
        ]
        step_settings = settings
        if dynamic_discharge is not None:
            stored_kwh = (
                max(0.0, soc_pct - settings.min_soc_pct)
                / 100
                * battery.capacity_kwh
                * battery.one_way_efficiency
            )
            threshold = discharge_price_threshold(
                prices_eur_kwh=horizon, stored_kwh=stored_kwh, settings=dynamic_discharge
            )
            if threshold is not None:
                step_settings = replace(
                    settings,
                    discharge_price_above=max(threshold, settings.charge_price_below + 0.01),
                )

        step = simulate_dispatch_cycle(
            phase_imports_w=sim_phases,
            battery_soc_pct=soc_pct,
            electricity_price=sample.electricity_price_eur_kwh,
            pv_power_w=sample.pv_power_w,
            actual_charge_power_w=max(0.0, -power_w),
            actual_discharge_power_w=max(0.0, power_w),
            max_charge_power_w=max_charge,
            max_discharge_power_w=max_discharge,
            interval_seconds=max(interval, 1e-9),
            tuning=tuning,
            settings=step_settings,
            memory=memory,
        )
        memory = step.memory
        plan = step.plan

        if (
            export is not None
            and plan.mode != "charge"
            and max_discharge is not None
            and max_discharge > 0
            and should_export_discharge(prices_eur_kwh=horizon, soc_pct=soc_pct, settings=export)
        ):
            plan = DispatchPlan("discharge", min(export.power_w, max_discharge), "export_discharge")

        if (
            net_charge is not None
            and plan.mode == "idle"
            and soc_pct < settings.max_soc_pct
            and max_charge is not None
            and max_charge > 0
        ):
            headroom_kwh = (100 - soc_pct) / 100 * battery.capacity_kwh
            if should_net_charge(
                prices_eur_kwh=horizon, headroom_kwh=headroom_kwh, settings=net_charge
            ):
                plan = DispatchPlan(
                    "charge", min(net_charge.charge_power_w, max_charge), "net_charge"
                )

        rows.append(
            {
                "timestamp": sample.timestamp.isoformat(),
                "base_net_w": sum(base),
                "sim_net_w": sum(sim_phases),
                "actual_net_w": sum(sample.phase_imports_w),
                "sim_battery_w": power_w,
                "actual_battery_w": sample.battery_power_w,
                "sim_soc_pct": soc_pct,
                "actual_soc_pct": sample.battery_soc_pct,
                "price_eur_kwh": sample.electricity_price_eur_kwh,
                "mode": plan.mode,
                "reason": plan.reason,
            }
        )

        # Apply the command after logging, so the next sample sees the plant response.
        target = {"charge": -plan.power_w, "discharge": plan.power_w}.get(plan.mode, 0.0)
        target = min(max(target, -battery.max_power_w), battery.max_power_w)
        step_limit = battery.ramp_w_per_s * max(interval, 1.0)
        power_w += min(max(target - power_w, -step_limit), step_limit)
        soc_pct = _advance_soc(soc_pct, power_w, max(interval, 1.0), battery, settings)
        if (soc_pct <= 0 and power_w > 0) or (soc_pct >= 100 and power_w < 0):
            power_w = 0.0

    return rows


def _quarter_prices(samples: list[ReplaySample]) -> dict[int, float]:
    """Map each quarter-hour (epoch // 900) in the trace to its price."""
    prices: dict[int, float] = {}
    for sample in samples:
        if sample.electricity_price_eur_kwh is not None:
            prices.setdefault(
                int(sample.timestamp.timestamp() // 900), sample.electricity_price_eur_kwh
            )
    return prices


def _advance_soc(
    soc_pct: float,
    power_w: float,
    seconds: float,
    battery: BatteryModel,
    settings: DispatchSettings,
) -> float:
    """Integrate stored energy; discharging takes more from the cell, charging stores less."""
    kwh = power_w * seconds / 3_600_000
    cell_kwh = kwh / battery.one_way_efficiency if kwh > 0 else kwh * battery.one_way_efficiency
    new_soc = soc_pct - 100 * cell_kwh / battery.capacity_kwh
    return min(max(new_soc, 0.0), 100.0)


def summarize(
    rows: list[dict[str, float | str]],
    interval_seconds: float,
    battery: BatteryModel,
    export_tax_eur_kwh: float = 0.0,
    cycle_cost_eur_kwh: float = 0.0,
) -> dict[str, float]:
    """Energy and cost totals; export earns price minus tax, stored energy uses the mean price."""
    hours = interval_seconds / 3600
    totals = {
        "base_cost": 0.0,
        "sim_cost": 0.0,
        "actual_cost": 0.0,
        "sim_charge_kwh": 0.0,
        "sim_discharge_kwh": 0.0,
        "actual_charge_kwh": 0.0,
        "actual_discharge_kwh": 0.0,
    }
    for row in rows:
        price = float(row["price_eur_kwh"])
        for key, field in (
            ("base_cost", "base_net_w"),
            ("sim_cost", "sim_net_w"),
            ("actual_cost", "actual_net_w"),
        ):
            net_kwh = float(row[field]) / 1000 * hours
            totals[key] += net_kwh * (price if net_kwh > 0 else price - export_tax_eur_kwh)
        for prefix in ("sim", "actual"):
            power = float(row[f"{prefix}_battery_w"]) / 1000 * hours
            totals[f"{prefix}_discharge_kwh"] += max(power, 0.0)
            totals[f"{prefix}_charge_kwh"] += max(-power, 0.0)
    totals["sim_soc_change_pct"] = float(rows[-1]["sim_soc_pct"]) - float(rows[0]["sim_soc_pct"])
    totals["actual_soc_change_pct"] = float(rows[-1]["actual_soc_pct"]) - float(
        rows[0]["actual_soc_pct"]
    )
    mean_price = sum(float(row["price_eur_kwh"]) for row in rows) / len(rows)
    kwh_per_soc_pct = battery.capacity_kwh * battery.one_way_efficiency / 100
    for prefix in ("sim", "actual"):
        stored_kwh = totals[f"{prefix}_soc_change_pct"] * kwh_per_soc_pct
        totals[f"{prefix}_cost_soc_adjusted"] = (
            totals[f"{prefix}_cost"]
            + totals[f"{prefix}_discharge_kwh"] * cycle_cost_eur_kwh
            - stored_kwh * mean_price
        )
        totals[f"{prefix}_full_cycles"] = totals[f"{prefix}_discharge_kwh"] / battery.capacity_kwh
    return totals


def main(argv: list[str] | None = None) -> int:
    """Run the closed-loop simulation and print a comparison summary."""
    parser = _build_parser()
    parser.description = __doc__
    parser.add_argument("--capacity-kwh", type=float, required=True)
    parser.add_argument("--one-way-efficiency", type=float, required=True)
    parser.add_argument("--ramp-w-per-s", type=float, required=True)
    parser.add_argument("--max-power-w", type=float, default=10000)
    parser.add_argument("--net-charge-margin", type=float, help="Min EUR/kWh gain; enables module")
    parser.add_argument("--net-charge-power-w", type=float, default=10000)
    parser.add_argument(
        "--dynamic-discharge-margin",
        type=float,
        help="Min EUR/kWh gain; enables price-aware discharge threshold",
    )
    parser.add_argument("--expected-load-w", type=float, default=1000)
    parser.add_argument("--round-trip-efficiency", type=float, default=0.92)
    parser.add_argument("--horizon-quarters", type=int, default=48)
    parser.add_argument("--cycle-cost", type=float, default=0.0, help="Wear EUR per kWh discharged")
    parser.add_argument("--export-tax", type=float, default=0.0, help="EUR per kWh exported")
    parser.add_argument("--export-margin", type=float, help="Min EUR/kWh gain; enables export")
    parser.add_argument("--export-reserve-soc", type=float, default=30.0)
    parser.add_argument("--export-power-w", type=float, default=10000)
    args = parser.parse_args(argv)
    try:
        tuning = PIDTuning(
            kp_charge=args.kp_charge,
            ki_charge=args.ki_charge,
            kd_charge=args.kd_charge,
            kp_discharge=args.kp_discharge,
            ki_discharge=args.ki_discharge,
            kd_discharge=args.kd_discharge,
            integral_limit_ws=args.integral_limit_ws,
            filter_alpha=args.filter_alpha,
            start_error_w=args.start_error_w,
            stop_error_w=args.stop_error_w,
        )
        samples = load_trace_csv(args.input_csv)
        battery = BatteryModel(
            args.capacity_kwh, args.one_way_efficiency, args.ramp_w_per_s, args.max_power_w
        )
        net_charge = (
            None
            if args.net_charge_margin is None
            else NetChargeSettings(
                args.net_charge_margin,
                args.round_trip_efficiency,
                args.net_charge_power_w,
                args.cycle_cost,
            )
        )
        dynamic_settings = (
            DischargeThresholdSettings(
                args.expected_load_w,
                args.round_trip_efficiency,
                args.cycle_cost,
                args.dynamic_discharge_margin,
            )
            if args.dynamic_discharge_margin is not None
            else None
        )
        export = (
            None
            if args.export_margin is None
            else ExportSettings(
                args.export_margin,
                args.round_trip_efficiency,
                args.export_tax,
                args.cycle_cost,
                args.export_reserve_soc,
                args.export_power_w,
            )
        )
        rows = simulate_closed_loop(
            samples,
            tuning=tuning,
            battery=battery,
            fuse_limit_amps=args.fuse_limit_amps,
            phase_headroom_w=args.phase_headroom_w,
            max_sample_gap_seconds=args.max_sample_gap_seconds,
            net_charge=net_charge,
            export=export,
            dynamic_discharge=dynamic_settings,
            horizon_quarters=args.horizon_quarters,
        )
        with args.output_csv.open("w", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=RESULT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    except (OSError, ValueError, csv.Error) as err:
        parser.error(str(err))

    interval = (samples[1].timestamp - samples[0].timestamp).total_seconds()
    totals = summarize(rows, interval, battery, args.export_tax, args.cycle_cost)
    for key, value in totals.items():
        print(f"{key}: {value:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
