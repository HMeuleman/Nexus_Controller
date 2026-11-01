"""Pure helpers for Nexus input validation."""

from __future__ import annotations

import math
from datetime import datetime


def parse_price_forecast(forecast: object) -> list[tuple[datetime, float]]:
    """Parse a Zonneplan-style `forecast` attribute into sorted (start, EUR/kWh) points.

    Zonneplan sometimes serves partial or missing data, so unusable entries are
    skipped and this never raises. Raw amounts are in 1e-7 EUR.
    """
    if not isinstance(forecast, (list, tuple)):
        return []

    points: dict[datetime, float] = {}
    for entry in forecast:
        if not isinstance(entry, dict):
            continue
        raw_start = entry.get("start_date") or entry.get("datetime")
        start = raw_start
        if isinstance(raw_start, str):
            try:
                start = datetime.fromisoformat(raw_start.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
        if not isinstance(start, datetime) or start.tzinfo is None:
            continue

        raw_price = entry.get("price_tax_included")
        if isinstance(raw_price, dict):
            raw_price = raw_price.get("amount")
        elif raw_price is None:
            raw_price = entry.get("electricity_price")
        try:
            price = float(raw_price)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price):
            continue
        points[start] = price * 1e-7 if abs(price) >= 100 else price
    return sorted(points.items())


_TAX_FREE_KEYS = (
    "price_tax_excluded",
    "price_excl_tax",
    "price_tax_free",
    "electricity_price_excl_tax",
    "price_without_tax",
)
_TAX_KEYS = ("tax", "tax_amount", "price_tax", "electricity_tax")


def _amount(raw: object) -> float | None:
    """Return a EUR/kWh amount from a raw value or an {"amount": ...} dict."""
    if isinstance(raw, dict):
        raw = raw.get("amount")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value * 1e-7 if abs(value) >= 100 else value


def parse_forecast_tax(forecast: object) -> dict[datetime, float]:
    """Return the per-slot tax (EUR/kWh) when the forecast reveals it, else nothing.

    Taxes are derived from a tax-free price or an explicit tax field; slots without
    either are omitted so the caller can fall back to its configured tax.
    """
    if not isinstance(forecast, (list, tuple)):
        return {}
    including = dict(parse_price_forecast(forecast))
    taxes: dict[datetime, float] = {}
    for entry in forecast:
        if not isinstance(entry, dict):
            continue
        raw_start = entry.get("start_date") or entry.get("datetime")
        if isinstance(raw_start, str):
            try:
                raw_start = datetime.fromisoformat(raw_start.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
        if not isinstance(raw_start, datetime) or raw_start not in including:
            continue
        tax = None
        for key in _TAX_FREE_KEYS:
            excluded = _amount(entry.get(key))
            if excluded is not None:
                tax = including[raw_start] - excluded
                break
        if tax is None:
            for key in _TAX_KEYS:
                tax = _amount(entry.get(key))
                if tax is not None:
                    break
        if tax is not None and tax >= 0:
            taxes[raw_start] = tax
    return taxes


def parse_energy_kwh(value: str | None, unit: str | None) -> float | None:
    """Return an energy state in kWh, or None for unusable values or units."""
    energy = parse_numeric_state(value)
    factor = {"kwh": 1.0, "wh": 0.001, "mwh": 1000.0}.get((unit or "").casefold())
    if energy is None or factor is None:
        return None
    return energy * factor


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


def sum_pv_power_states(states: list[tuple[str | None, str | None]]) -> float | None:
    """Sum PV sources, interpreting a valid-unit `unknown` state as zero output.

    A missing state object, `unavailable` state, malformed value, or unsupported
    unit remains unknown so a real entity or integration failure is not hidden.
    """
    total = 0.0
    for value, unit in states:
        if value is None or value == "unavailable":
            return None

        normalized_unit = unit.casefold() if unit else ""
        if value == "unknown" and not normalized_unit:
            continue
        if normalized_unit == "kw":
            multiplier = 1000
        elif normalized_unit == "w":
            multiplier = 1
        else:
            return None

        if value == "unknown":
            continue
        power = parse_numeric_state(value)
        if power is None:
            return None
        total += power * multiplier
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
