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
        normalized_unit = unit.casefold() if unit else ""
        if normalized_unit == "kw":
            power *= 1000
        elif normalized_unit != "w":
            return None
        total += power
    return total


def calculate_phase_power_limits(
    phase_imports_w: tuple[float | None, float | None, float | None],
    fuse_limit_amps: float,
    headroom_watts: float,
    max_battery_power_w: float = 10_000,
) -> tuple[float | None, float | None]:
    """Calculate balanced 3-phase battery charge/discharge caps in watts.

    Phase readings use positive values for grid import and negative values for export.
    """
    if any(value is None for value in phase_imports_w):
        return None, None
    if fuse_limit_amps <= 0 or headroom_watts < 0 or max_battery_power_w <= 0:
        return None, None

    phase_cap_w = fuse_limit_amps * 230 - headroom_watts
    if phase_cap_w <= 0:
        return 0.0, 0.0

    phases = tuple(value for value in phase_imports_w if value is not None)
    charge_per_phase = min(phase_cap_w - value for value in phases)
    discharge_per_phase = min(phase_cap_w + value for value in phases)

    max_charge = min(max_battery_power_w, max(0.0, charge_per_phase * 3))
    max_discharge = min(max_battery_power_w, max(0.0, discharge_per_phase * 3))
    return max_charge, max_discharge


def unique_entity_id_with_suffix(entity_ids: list[str], suffix: str) -> str | None:
    """Return a unique entity ID containing a known entity-name suffix."""
    matches = [entity_id for entity_id in entity_ids if entity_id.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None
