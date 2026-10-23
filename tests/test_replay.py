"""Tests for historical CSV shadow replay."""

import csv
from datetime import datetime

import pytest

from custom_components.nexus_controller.control import PIDTuning
from sim.replay import (
    TRACE_COLUMNS,
    ReplaySample,
    load_trace_csv,
    replay_samples,
    write_results_csv,
)

TUNING = PIDTuning(
    kp_charge=0.5,
    ki_charge=0,
    kd_charge=0,
    kp_discharge=0.5,
    ki_discharge=0,
    kd_discharge=0,
    integral_limit_ws=10_000,
    filter_alpha=1,
)


def sample(second: int, *, net: float = 50) -> ReplaySample:
    """Create a representative timestamped HA measurement row."""
    return ReplaySample(
        timestamp=datetime.fromisoformat(f"2026-10-01T12:00:{second:02d}+02:00"),
        phase_imports_w=(net, net, net),
        pv_power_w=0,
        battery_soc_pct=50,
        electricity_price_eur_kwh=0.35,
        battery_power_w=850,
    )


def test_replay_uses_actual_samples_and_returns_shadow_proposals():
    """A real-trace replay uses recorded battery feedback and phase-safe caps."""
    results = replay_samples([sample(0), sample(10)], tuning=TUNING)

    assert len(results) == 2
    assert results[0].plan.mode == "discharge"
    assert results[0].plan.power_w == 925
    assert results[0].max_discharge_power_w is not None
    assert results[0].max_discharge_power_w <= 10_000


def test_negative_battery_power_is_treated_as_charging_feedback():
    """Negative signed battery power feeds the charge direction of the PID."""
    charging = ReplaySample(
        timestamp=datetime.fromisoformat("2026-10-01T12:00:00+02:00"),
        phase_imports_w=(-200, -200, -200),
        pv_power_w=3000,
        battery_soc_pct=50,
        electricity_price_eur_kwh=0.25,
        battery_power_w=-100,
    )
    later = ReplaySample(
        timestamp=datetime.fromisoformat("2026-10-01T12:00:10+02:00"),
        phase_imports_w=(-200, -200, -200),
        pv_power_w=3000,
        battery_soc_pct=50,
        electricity_price_eur_kwh=0.25,
        battery_power_w=-100,
    )

    results = replay_samples([charging, later], tuning=TUNING)

    assert (results[0].plan.mode, results[0].plan.power_w) == ("charge", 400)


def test_replay_resets_after_large_sample_gap():
    """A gap longer than the configured maximum must not bridge PID memory."""
    results = replay_samples(
        [sample(0), sample(10), sample(40)],
        tuning=TUNING,
        max_sample_gap_seconds=15,
    )

    assert results[2].plan.mode == "idle"
    assert results[2].plan.reason == "sample_gap"


def test_csv_loader_parses_timezone_and_blank_measurements(tmp_path):
    """The normalized CSV loader accepts blanks as unknown measurements."""
    csv_path = tmp_path / "trace.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=TRACE_COLUMNS)
        writer.writeheader()
        writer.writerow(
            {
                "timestamp": "2026-10-01T12:00:00+02:00",
                "grid_power_l1_w": "100",
                "grid_power_l2_w": "200",
                "grid_power_l3_w": "300",
                "pv_power_w": "0",
                "battery_soc_pct": "50",
                "electricity_price_eur_kwh": "0.35",
                "battery_power_w": "",
            }
        )
        writer.writerow(
            {
                "timestamp": "2026-10-01T12:00:10+02:00",
                "grid_power_l1_w": "110",
                "grid_power_l2_w": "210",
                "grid_power_l3_w": "310",
                "pv_power_w": "0",
                "battery_soc_pct": "50",
                "electricity_price_eur_kwh": "0.35",
                "battery_power_w": "-850",
            }
        )

    samples = load_trace_csv(csv_path)

    assert len(samples) == 2
    assert samples[0].phase_imports_w == (100, 200, 300)
    assert samples[0].battery_power_w is None
    assert samples[1].battery_power_w == -850
    assert samples[0].timestamp.utcoffset().total_seconds() == 7200


def test_csv_loader_requires_timezone_and_all_columns(tmp_path):
    """Ambiguous local timestamps and incomplete exports are rejected."""
    csv_path = tmp_path / "trace.csv"
    csv_path.write_text("timestamp,grid_power_l1_w\n2026-10-01T12:00:00,100\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Missing CSV columns"):
        load_trace_csv(csv_path)


def test_replay_output_csv_contains_proposal_columns(tmp_path):
    """Replay output keeps the measurements and adds proposed mode/power/reason."""
    output_path = tmp_path / "proposals.csv"
    results = replay_samples([sample(0), sample(10)], tuning=TUNING)
    with output_path.open("w", encoding="utf-8", newline="") as output_file:
        write_results_csv(results, output_file)

    with output_path.open(encoding="utf-8", newline="") as output_file:
        rows = list(csv.DictReader(output_file))

    assert len(rows) == 2
    assert rows[0]["proposed_mode"] == "discharge"
    assert rows[0]["proposed_power_w"] == "925.0"
    assert rows[0]["reason"] == "closed_loop_pid_step"
