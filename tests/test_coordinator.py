"""Tests for safe input parsing in the Nexus coordinator."""

import pytest

from custom_components.nexus_controller.utils import (
    calculate_phase_power_limits,
    parse_numeric_state,
    sum_power_states,
    sum_pv_power_states,
    unique_entity_id_with_suffix,
)


@pytest.mark.parametrize(
    ("state", "expected"),
    [("123.5", 123.5), ("unknown", None), ("unavailable", None), ("not-a-number", None)],
)
def test_parse_numeric_state_converts_numeric_values_and_ignores_invalid(state, expected):
    """Unavailable or nonnumeric states must not become control values."""
    assert parse_numeric_state(state) == expected


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([], 0),
        ([("500", "W"), ("1.2", "kW")], 1700),
        ([("0.765", "KW")], 765),
        ([("0.765", "kW")], 765),
        ([("500", "W"), ("unavailable", "W")], None),
        ([("500", None)], None),
        ([("500", "W"), ("1", "MW")], None),
    ],
)
def test_sum_power_states_normalizes_units_and_propagates_invalid_values(states, expected):
    """PV aggregation should not silently undercount missing or unsupported sensor values."""
    assert sum_power_states(states) == expected


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([("unknown", "W")], 0),
        ([("unknown", None)], 0),
        ([("unknown", "")], 0),
        ([("unknown", "kW"), ("0.8", "kW")], 800),
        ([("100", "W"), ("unavailable", "W")], None),
        ([("100", "W"), (None, "W")], None),
        ([("unknown", "MW")], None),
    ],
)
def test_sum_pv_power_states_treats_no_production_as_zero_but_keeps_faults(states, expected):
    """Normal PV `unknown` is zero, while unavailable or invalid sources remain unknown."""
    assert sum_pv_power_states(states) == expected


def test_phase_power_limit_protects_exporting_phase():
    """A phase exporting 4 kW limits balanced battery discharge despite net import."""
    charge_limit, discharge_limit = calculate_phase_power_limits(
        (-4000, 5000, 5000),
        fuse_limit_amps=25,
        headroom_watts=1000,
    )

    assert charge_limit == 0
    assert discharge_limit == 2250


def test_phase_power_limit_caps_nexus_power_at_ten_kw():
    """The fixed Nexus battery power limit must cap larger fuse capacity."""
    charge_limit, discharge_limit = calculate_phase_power_limits(
        (0, 0, 0),
        fuse_limit_amps=35,
        headroom_watts=0,
    )

    assert charge_limit == 10_000
    assert discharge_limit == 10_000


def test_phase_power_limit_is_unknown_if_any_phase_is_unknown():
    """Do not calculate a safe limit when one phase sensor is unavailable."""
    assert calculate_phase_power_limits((100, None, -100), 25, 1000) == (None, None)


def test_unique_entity_id_match_requires_single_suffix_match():
    """Auto-selection should not guess when an entity suffix is ambiguous."""
    assert (
        unique_entity_id_with_suffix(
            ["number.solis_rc_force_battery_charge_power"],
            "rc_force_battery_charge_power",
        )
        == "number.solis_rc_force_battery_charge_power"
    )
    assert (
        unique_entity_id_with_suffix(
            [
                "number.solis_rc_force_battery_charge_power",
                "number.other_rc_force_battery_charge_power",
            ],
            "rc_force_battery_charge_power",
        )
        is None
    )
