from datetime import UTC, datetime, timedelta

import pytest

from sim.build_trace import build_rows, drop_ranges, mean_over

START = datetime(2026, 10, 1, tzinfo=UTC)
ENTITIES = {k: k for k in ("l1", "l2", "l3", "pv", "soc", "price", "battery")}


def _series(value, offset=0):
    return [(START + timedelta(seconds=offset), value)]


def test_build_rows_scales_forward_fills_and_defaults_pv_to_zero():
    series = {key: [*_series(1.0), *_series(1.0, 20)] for key in ENTITIES}
    series["pv"] = _series(None)
    series["battery"] = [(START, -100.0), (START + timedelta(seconds=20), 50.0)]

    rows = build_rows(
        series,
        entities=ENTITIES,
        scales={"grid": -1000, "pv": 1, "battery": 1},
        interval_seconds=10,
    )

    assert [row["battery_power_w"] for row in rows] == [-100.0, -100.0, 50.0]
    assert rows[0]["grid_power_l1_w"] == -1000.0
    assert rows[0]["pv_power_w"] == 0.0


def test_mean_over_weights_steps_by_duration_and_ignores_unknown_time():
    start = START
    series = [
        (start, 100.0),
        (start + timedelta(minutes=15), 300.0),
        (start + timedelta(minutes=30), None),
        (start + timedelta(minutes=45), 500.0),
    ]
    times = [t for t, _ in series]

    result = mean_over(series, times, start, start + timedelta(hours=1))

    # 15 min at 100, 15 min at 300, 15 min unknown, 15 min at 500.
    assert result == pytest.approx(300.0)
    assert mean_over(series, times, start - timedelta(hours=2), start - timedelta(hours=1)) is None


def test_second_solar_sensor_is_summed_with_the_first():
    entities = {**ENTITIES, "pv2": "pv2"}
    series = {key: [*_series(1.0), *_series(1.0, 20)] for key in entities}
    series["pv"] = [*_series(300.0), *_series(300.0, 20)]
    series["pv2"] = [*_series(200.0), *_series(None, 10), *_series(200.0, 20)]

    rows = build_rows(
        series,
        entities=entities,
        scales={"grid": 1000, "pv": 1, "battery": 1},
        interval_seconds=10,
    )

    assert [row["pv_power_w"] for row in rows] == [500.0, 300.0, 500.0]


def test_drop_ranges_removes_rows_in_ranges():
    rows = [{"timestamp": (START + timedelta(hours=h)).isoformat()} for h in range(5)]
    result = drop_ranges(rows, [(START + timedelta(hours=1), START + timedelta(hours=3))])
    assert [r["timestamp"] for r in result] == [
        rows[0]["timestamp"],
        rows[3]["timestamp"],
        rows[4]["timestamp"],
    ]
