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
