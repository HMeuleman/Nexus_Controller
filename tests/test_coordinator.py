"""Tests for safe input parsing in the Nexus coordinator."""

import pytest

from custom_components.nexus_controller.utils import parse_numeric_state


@pytest.mark.parametrize(
    ("state", "expected"),
    [("123.5", 123.5), ("unknown", None), ("unavailable", None), ("not-a-number", None)],
)
def test_parse_numeric_state_converts_numeric_values_and_ignores_invalid(state, expected):
    """Unavailable or nonnumeric states must not become control values."""
    assert parse_numeric_state(state) == expected
