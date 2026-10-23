"""Replay exported Home Assistant history through the offline EMS simulator."""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from custom_components.nexus_controller.control import PIDMemory, PIDTuning, simulate_dispatch_cycle
from custom_components.nexus_controller.dispatch import (
    DEFAULT_DISPATCH_SETTINGS,
    DispatchPlan,
    DispatchSettings,
)
from custom_components.nexus_controller.utils import (
    calculate_phase_power_limits,
    parse_numeric_state,
)

TRACE_COLUMNS = (
    "timestamp",
    "grid_power_l1_w",
    "grid_power_l2_w",
    "grid_power_l3_w",
    "pv_power_w",
    "battery_soc_pct",
    "electricity_price_eur_kwh",
    "actual_charge_power_w",
    "actual_discharge_power_w",
)
OUTPUT_COLUMNS = (
    *TRACE_COLUMNS,
    "max_charge_power_w",
    "max_discharge_power_w",
    "proposed_mode",
    "proposed_power_w",
    "reason",
)


@dataclass(frozen=True)
class ReplaySample:
    """One aligned historical measurement sample."""

    timestamp: datetime
    phase_imports_w: tuple[float | None, float | None, float | None]
    pv_power_w: float | None
    battery_soc_pct: float | None
    electricity_price_eur_kwh: float | None
    actual_charge_power_w: float | None
    actual_discharge_power_w: float | None


@dataclass(frozen=True)
class ReplayResult:
    """The planner's proposed action for one historical sample."""

    sample: ReplaySample
    max_charge_power_w: float | None
    max_discharge_power_w: float | None
    plan: DispatchPlan


def load_trace_csv(path: Path) -> list[ReplaySample]:
    """Load a normalized, aligned Home Assistant history CSV."""
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        missing_columns = set(TRACE_COLUMNS) - set(reader.fieldnames or ())
        if missing_columns:
            raise ValueError(f"Missing CSV columns: {', '.join(sorted(missing_columns))}")

        samples = [
            ReplaySample(
                timestamp=_parse_timestamp(row["timestamp"]),
                phase_imports_w=(
                    parse_numeric_state(row["grid_power_l1_w"]),
                    parse_numeric_state(row["grid_power_l2_w"]),
                    parse_numeric_state(row["grid_power_l3_w"]),
                ),
                pv_power_w=parse_numeric_state(row["pv_power_w"]),
                battery_soc_pct=parse_numeric_state(row["battery_soc_pct"]),
                electricity_price_eur_kwh=parse_numeric_state(row["electricity_price_eur_kwh"]),
                actual_charge_power_w=parse_numeric_state(row["actual_charge_power_w"]),
                actual_discharge_power_w=parse_numeric_state(row["actual_discharge_power_w"]),
            )
            for row in reader
        ]
    _validate_timestamps(samples)
    return samples


def replay_samples(
    samples: list[ReplaySample],
    *,
    tuning: PIDTuning,
    settings: DispatchSettings = DEFAULT_DISPATCH_SETTINGS,
    fuse_limit_amps: float = 25,
    phase_headroom_w: float = 1000,
    max_sample_gap_seconds: float = 900,
) -> list[ReplayResult]:
    """Replay aligned history without issuing commands or modelling plant response."""
    _validate_timestamps(samples)
    if len(samples) < 2:
        raise ValueError("Replay requires at least two timestamped samples")
    if not math.isfinite(max_sample_gap_seconds) or max_sample_gap_seconds <= 0:
        raise ValueError("max_sample_gap_seconds must be positive and finite")

    results: list[ReplayResult] = []
    memory = PIDMemory()
    for index, sample in enumerate(samples):
        if index == 0:
            interval_seconds = (samples[1].timestamp - sample.timestamp).total_seconds()
        else:
            interval_seconds = (sample.timestamp - samples[index - 1].timestamp).total_seconds()

        if interval_seconds > max_sample_gap_seconds:
            memory = PIDMemory()
            results.append(
                ReplayResult(
                    sample,
                    None,
                    None,
                    DispatchPlan("idle", 0.0, "sample_gap"),
                )
            )
            continue

        if interval_seconds <= 0:
            raise ValueError("Replay timestamps must be strictly increasing")

        max_charge, max_discharge = calculate_phase_power_limits(
            sample.phase_imports_w,
            fuse_limit_amps,
            phase_headroom_w,
        )
        step = simulate_dispatch_cycle(
            phase_imports_w=sample.phase_imports_w,
            battery_soc_pct=sample.battery_soc_pct,
            electricity_price=sample.electricity_price_eur_kwh,
            pv_power_w=sample.pv_power_w,
            actual_charge_power_w=sample.actual_charge_power_w,
            actual_discharge_power_w=sample.actual_discharge_power_w,
            max_charge_power_w=max_charge,
            max_discharge_power_w=max_discharge,
            interval_seconds=interval_seconds,
            tuning=tuning,
            settings=settings,
            memory=memory,
        )
        memory = step.memory
        results.append(ReplayResult(sample, max_charge, max_discharge, step.plan))

    return results


def write_results_csv(results: list[ReplayResult], stream: TextIO) -> None:
    """Write original samples alongside safe caps and proposed actions."""
    writer = csv.DictWriter(stream, fieldnames=OUTPUT_COLUMNS)
    writer.writeheader()
    for result in results:
        sample = result.sample
        writer.writerow(
            {
                "timestamp": sample.timestamp.isoformat(),
                "grid_power_l1_w": sample.phase_imports_w[0],
                "grid_power_l2_w": sample.phase_imports_w[1],
                "grid_power_l3_w": sample.phase_imports_w[2],
                "pv_power_w": sample.pv_power_w,
                "battery_soc_pct": sample.battery_soc_pct,
                "electricity_price_eur_kwh": sample.electricity_price_eur_kwh,
                "actual_charge_power_w": sample.actual_charge_power_w,
                "actual_discharge_power_w": sample.actual_discharge_power_w,
                "max_charge_power_w": result.max_charge_power_w,
                "max_discharge_power_w": result.max_discharge_power_w,
                "proposed_mode": result.plan.mode,
                "proposed_power_w": result.plan.power_w,
                "reason": result.plan.reason,
            }
        )


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp with an explicit timezone."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as err:
        raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}") from err
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {value!r}")
    return parsed


def _validate_timestamps(samples: list[ReplaySample]) -> None:
    """Require monotonically increasing timestamps before stateful replay."""
    for previous, current in zip(samples, samples[1:], strict=False):
        if current.timestamp <= previous.timestamp:
            raise ValueError("Replay timestamps must be strictly increasing")


def _build_parser() -> argparse.ArgumentParser:
    """Build the replay command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path, help="Normalized HA history CSV")
    parser.add_argument("output_csv", type=Path, help="Path for proposals CSV")
    parser.add_argument("--kp-charge", type=float, required=True)
    parser.add_argument("--ki-charge", type=float, required=True)
    parser.add_argument("--kd-charge", type=float, required=True)
    parser.add_argument("--kp-discharge", type=float, required=True)
    parser.add_argument("--ki-discharge", type=float, required=True)
    parser.add_argument("--kd-discharge", type=float, required=True)
    parser.add_argument("--integral-limit-ws", type=float, required=True)
    parser.add_argument("--filter-alpha", type=float, required=True)
    parser.add_argument("--start-error-w", type=float, default=15)
    parser.add_argument("--stop-error-w", type=float, default=8)
    parser.add_argument("--fuse-limit-amps", type=float, default=25)
    parser.add_argument("--phase-headroom-w", type=float, default=1000)
    parser.add_argument("--max-sample-gap-seconds", type=float, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a historical shadow replay and write proposed actions to CSV."""
    parser = _build_parser()
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
        results = replay_samples(
            samples,
            tuning=tuning,
            fuse_limit_amps=args.fuse_limit_amps,
            phase_headroom_w=args.phase_headroom_w,
            max_sample_gap_seconds=args.max_sample_gap_seconds,
        )
        with args.output_csv.open("w", encoding="utf-8", newline="") as output_file:
            write_results_csv(results, output_file)
    except (OSError, ValueError, csv.Error) as err:
        parser.error(str(err))

    mode_counts = {
        mode: sum(result.plan.mode == mode for result in results)
        for mode in ("idle", "charge", "discharge")
    }
    print(
        f"Wrote {len(results)} shadow proposals to {args.output_csv}: "
        f"{mode_counts['charge']} charge, {mode_counts['discharge']} discharge, "
        f"{mode_counts['idle']} idle. No inverter commands were sent.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
