from datetime import UTC, datetime, timedelta

from sim.build_trace import build_rows

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
