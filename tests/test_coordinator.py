"""Tests for safe input parsing in the Nexus coordinator."""

import pytest

from custom_components.nexus_controller.utils import parse_numeric_state, sum_power_states


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
        ([("0.765", "kW")], 765),
        ([("500", "W"), ("unavailable", "W")], None),
        ([("500", "W"), ("1", "MW")], None),
    ],
)
def test_sum_power_states_normalizes_units_and_propagates_invalid_values(states, expected):
    """PV aggregation should not silently undercount missing or unsupported sensor values."""
    assert sum_power_states(states) == expected
