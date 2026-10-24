"""Merge a long-format Home Assistant history export into an aligned replay trace."""

from __future__ import annotations

import argparse
import csv
from bisect import bisect_right
from datetime import datetime, timedelta
from pathlib import Path

from custom_components.nexus_controller.utils import parse_numeric_state
from sim.replay import TRACE_COLUMNS, _parse_timestamp

Series = list[tuple[datetime, float | None]]


def load_series(path: Path) -> dict[str, Series]:
    """Read `entity_id,state,last_changed` rows into time-sorted series per entity."""
    series: dict[str, Series] = {}
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            series.setdefault(row["entity_id"], []).append(
                (_parse_timestamp(row["last_changed"]), parse_numeric_state(row["state"]))
            )
    for points in series.values():
        points.sort(key=lambda point: point[0])
    return series


def value_at(points: Series, times: list[datetime], moment: datetime) -> float | None:
    """Return the last known state at `moment`, or None before the first change."""
    index = bisect_right(times, moment) - 1
    return None if index < 0 else points[index][1]


def mean_over(
    points: Series, times: list[datetime], start: datetime, end: datetime
) -> float | None:
    """Return the time-weighted mean of a step series over [start, end), ignoring unknown time."""
    index = bisect_right(times, start) - 1
    cursor = start
    total = 0.0
    covered = 0.0
    while cursor < end:
        next_change = times[index + 1] if index + 1 < len(times) else end
        segment_end = min(next_change, end)
        if index >= 0 and points[index][1] is not None:
            seconds = (segment_end - cursor).total_seconds()
            total += points[index][1] * seconds
            covered += seconds
        cursor = segment_end
        index += 1
    return total / covered if covered else None


def build_rows(
    series: dict[str, Series],
    *,
    entities: dict[str, str],
    scales: dict[str, float],
    interval_seconds: float,
    average: bool = False,
) -> list[dict[str, str | float]]:
    """Resample series on a fixed grid and scale to replay units.

    Power series are sampled, or averaged over each interval when `average` is set
    (needed for hourly statistics); SOC and price are always the value at the start.
    """
    times = {key: [t for t, _ in series[entity]] for key, entity in entities.items()}
    points = {key: series[entity] for key, entity in entities.items()}
    start = max(times[key][0] for key in ("l1", "l2", "l3", "soc", "price", "battery"))
    end = min(times[key][-1] for key in ("l1", "l2", "l3", "soc", "price", "battery"))

    rows: list[dict[str, str | float]] = []
    moment = start
    while moment <= end:
        values = {key: value_at(points[key], times[key], moment) for key in entities}
        if average:
            window_end = moment + timedelta(seconds=interval_seconds)
            for key in ("l1", "l2", "l3", "pv", "pv2", "battery"):
                if key in entities:
                    values[key] = mean_over(points[key], times[key], moment, window_end)
        required = ("l1", "l2", "l3", "soc", "price", "battery")
        if all(values[key] is not None for key in required):
            # Unavailable solar means the inverter is offline, which produces nothing.
            pv = sum(values[key] or 0.0 for key in ("pv", "pv2") if key in entities)
            rows.append(
                {
                    "timestamp": moment.isoformat(),
                    "grid_power_l1_w": values["l1"] * scales["grid"],
                    "grid_power_l2_w": values["l2"] * scales["grid"],
                    "grid_power_l3_w": values["l3"] * scales["grid"],
                    "pv_power_w": pv * scales["pv"],
                    "battery_soc_pct": values["soc"],
                    "electricity_price_eur_kwh": values["price"],
                    "battery_power_w": values["battery"] * scales["battery"],
                }
            )
        moment += timedelta(seconds=interval_seconds)
    return rows


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("--l1", default="sensor.electricity_l1_3")
    parser.add_argument("--l2", default="sensor.electricity_l2_3")
    parser.add_argument("--l3", default="sensor.electricity_l3_3")
    parser.add_argument("--pv", default="sensor.solaredge_schuur_i2_ac_power")
    parser.add_argument("--pv2", help="Second solar sensor, summed with --pv")
    parser.add_argument("--soc", default="sensor.solis_battery_soc")
    parser.add_argument(
        "--price", default="sensor.zonneplan_current_quarter_hourly_electricity_tariff"
    )
    parser.add_argument("--battery", default="sensor.solis_battery_power_net")
    parser.add_argument("--grid-scale", type=float, required=True, help="Factor to W (kW=1000)")
    parser.add_argument("--pv-scale", type=float, required=True, help="Factor to W")
    parser.add_argument("--battery-scale", type=float, required=True, help="Factor to W")
    parser.add_argument(
        "--grid-sign",
        type=float,
        choices=(-1, 1),
        required=True,
        help="1 if positive grid values mean import, -1 if positive means export",
    )
    parser.add_argument("--interval-seconds", type=float, default=10)
    parser.add_argument(
        "--average", action="store_true", help="Average power series over each interval"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write an aligned trace CSV for `python -m sim.replay`."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    entities = {
        "l1": args.l1,
        "l2": args.l2,
        "l3": args.l3,
        "pv": args.pv,
        "soc": args.soc,
        "price": args.price,
        "battery": args.battery,
    }
    if args.pv2:
        entities["pv2"] = args.pv2
    try:
        series = load_series(args.input_csv)
        missing = [entity for entity in entities.values() if entity not in series]
        if missing:
            parser.error(f"Entities missing from CSV: {', '.join(missing)}")
        if args.interval_seconds <= 0:
            parser.error("--interval-seconds must be positive")
        rows = build_rows(
            series,
            entities=entities,
            scales={
                "grid": args.grid_scale * args.grid_sign,
                "pv": args.pv_scale,
                "battery": args.battery_scale,
            },
            interval_seconds=args.interval_seconds,
            average=args.average,
        )
        with args.output_csv.open("w", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=TRACE_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    except (OSError, ValueError, csv.Error) as err:
        parser.error(str(err))
    print(f"Wrote {len(rows)} aligned samples to {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
