from datetime import UTC, datetime, timedelta

from custom_components.nexus_controller.control import PIDTuning
from sim.closed_loop import BatteryModel, simulate_closed_loop
from sim.replay import ReplaySample

TUNING = PIDTuning(0.5, 0.0, 0.0, 0.5, 0.0, 0.0, 1000.0, 1.0)
BATTERY = BatteryModel(20.0, 1.0, 5000.0, 10000.0)


def _samples(count):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    return [
        ReplaySample(start + timedelta(seconds=10 * i), (1000.0,) * 3, 0.0, 50.0, 0.40, 0.0)
        for i in range(count)
    ]


def test_expensive_import_is_reduced_by_simulated_discharge():
    rows = simulate_closed_loop(_samples(30), tuning=TUNING, battery=BATTERY)

    assert rows[0]["sim_battery_w"] == 0.0
    assert rows[-1]["sim_battery_w"] > 0
    assert rows[-1]["sim_net_w"] < rows[0]["sim_net_w"] == 3000.0
    assert rows[-1]["sim_soc_pct"] < 50.0
