"""Pure helpers for Nexus input validation."""

from __future__ import annotations


def parse_numeric_state(value: str | None) -> float | None:
    """Return a finite numeric entity state, or None when it is not usable."""
    if value is None or value in ("unknown", "unavailable"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def sum_power_states(states: list[tuple[str | None, str | None]]) -> float | None:
    """Sum power sensor states in watts; return None if a configured value is invalid."""
    total = 0.0
    for value, unit in states:
        power = parse_numeric_state(value)
        if power is None:
            return None
        if unit == "kW":
            power *= 1000
        elif unit not in ("W", "w"):
            return None
        total += power
    return total
