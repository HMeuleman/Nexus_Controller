"""Track battery cycles and an estimate of what the battery earned (pure, persistable)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from datetime import datetime

from .charging import inverter_efficiency

# Longer gaps (restart, outage) are not integrated, the next sample just re-baselines.
MAX_INTEGRATION_GAP_S = 120.0
MIN_INVENTORY_KWH = 0.5


@dataclass
class TrackerState:
    """Everything that must survive a restart."""

    cycles_total: float = 0.0
    cycles_today: float = 0.0
    earned_total: float = 0.0
    earned_today: float = 0.0
    stored_value: float = 0.10  # EUR per kWh currently in the battery
    day: str = ""
    avg_daytime_load_w: float | None = None


class BatteryTracker:
    """Integrates battery power over time at the price each kWh was bought or sold for."""

    def __init__(self, capacity_kwh: float, state: TrackerState | None = None) -> None:
        self.capacity_kwh = capacity_kwh
        self.state = state or TrackerState()
        self._last_time: datetime | None = None
        self._last_soc: float | None = None

    @staticmethod
    def state_from_dict(data: object) -> TrackerState:
        """Rebuild state from stored data, ignoring unknown or invalid fields."""
        if not isinstance(data, dict):
            return TrackerState()
        names = {field.name for field in fields(TrackerState)}
        clean = {
            key: value
            for key, value in data.items()
            if key in names and (isinstance(value, str) or value is None or math.isfinite(value))
        }
        return TrackerState(**clean)

    def as_dict(self) -> dict:
        """Return the state for storage."""
        return asdict(self.state)

    def update(
        self,
        *,
        now: datetime,
        day: str,
        soc_pct: float | None,
        battery_power_w: float | None,
        grid_import_w: float | None,
        import_price: float | None,
        export_price: float | None,
    ) -> None:
        """Advance the tracker to `now`; battery power is positive when discharging."""
        state = self.state
        if state.day != day:
            state.day = day
            state.cycles_today = 0.0
            state.earned_today = 0.0

        previous_time, previous_soc = self._last_time, self._last_soc
        self._last_time = now
        if soc_pct is not None:
            self._last_soc = soc_pct
            if previous_soc is not None and soc_pct > previous_soc:
                gained = (soc_pct - previous_soc) / 100
                state.cycles_today += gained
                state.cycles_total += gained

        if (
            previous_time is None
            or battery_power_w is None
            or grid_import_w is None
            or import_price is None
            or export_price is None
        ):
            return
        seconds = (now - previous_time).total_seconds()
        if not 0 < seconds <= MAX_INTEGRATION_GAP_S or battery_power_w == 0:
            return
        hours = seconds / 3600
        if battery_power_w < 0:
            self._charge(
                -battery_power_w, hours, grid_import_w, import_price, export_price, previous_soc
            )
        else:
            self._discharge(battery_power_w, hours, grid_import_w, import_price, export_price)

    def _charge(
        self,
        power_w: float,
        hours: float,
        grid_import_w: float,
        import_price: float,
        export_price: float,
        previous_soc: float | None,
    ) -> None:
        """Add energy to the battery at the blend of grid price and forgone export price."""
        energy_kwh = power_w / 1000 * hours
        grid_share = min(max(grid_import_w, 0.0), power_w) / power_w
        cost = energy_kwh * (grid_share * import_price + (1 - grid_share) * export_price)
        stored_kwh = energy_kwh * inverter_efficiency(power_w)
        inventory = max((previous_soc or 0.0) / 100 * self.capacity_kwh, MIN_INVENTORY_KWH)
        self.state.stored_value = (self.state.stored_value * inventory + cost) / (
            inventory + stored_kwh
        )

    def _discharge(
        self,
        power_w: float,
        hours: float,
        grid_import_w: float,
        import_price: float,
        export_price: float,
    ) -> None:
        """Book delivered energy at what it saved or sold for, minus what it cost to store."""
        energy_kwh = power_w / 1000 * hours
        export_share = min(max(-grid_import_w, 0.0), power_w) / power_w
        revenue = energy_kwh * (export_share * export_price + (1 - export_share) * import_price)
        basis = energy_kwh / inverter_efficiency(power_w) * self.state.stored_value
        earned = revenue - basis
        self.state.earned_today += earned
        self.state.earned_total += earned
