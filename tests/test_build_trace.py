from datetime import UTC, datetime, timedelta

import pytest

from sim.build_trace import build_rows, mean_over

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
