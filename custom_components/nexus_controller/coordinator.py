"""Coordinator for configured Nexus sensors."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_FUSE_LIMIT,
    CONF_PHASE_HEADROOM,
    DEFAULT_FUSE_LIMIT_A,
    DEFAULT_PHASE_HEADROOM_W,
    DOMAIN,
    INPUT_SENSOR_ENTITIES,
    MAX_BATTERY_POWER_W,
    PHASE_POWER_ENTITIES,
    PV_POWER_ENTITIES,
)
from .utils import (
    calculate_phase_power_limits,
    parse_numeric_state,
    sum_power_states,
    sum_pv_power_states,
)

_LOGGER = logging.getLogger(__name__)


class NexusCoordinator(DataUpdateCoordinator[dict[str, float | None]]):
    """Read configured HA entities; this initial version never commands hardware."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=10),
        )
        self.entry = entry

    async def _async_update_data(self) -> dict[str, float | None]:
        """Read selected entity values and represent unavailable states as None."""
        data: dict[str, float | None] = {}
        for key in INPUT_SENSOR_ENTITIES:
            entity_id = self.entry.options.get(key, self.entry.data.get(key))
            state = self.hass.states.get(entity_id) if entity_id else None
            if key in PHASE_POWER_ENTITIES:
                data[key] = sum_power_states(
                    [
                        (
                            state.state if state else None,
                            state.attributes.get("unit_of_measurement") if state else None,
                        )
                    ]
                )
            else:
                data[key] = parse_numeric_state(state.state if state is not None else None)
        pv_states: list[tuple[str | None, str | None]] = []
        for key in PV_POWER_ENTITIES:
            entity_id = self.configured_entity_id(key)
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            pv_states.append(
                (
                    state.state if state is not None else None,
                    state.attributes.get("unit_of_measurement") if state is not None else None,
                )
            )
        data[PV_POWER_ENTITIES[0]] = sum_pv_power_states(pv_states)
        limits = calculate_phase_power_limits(
            tuple(data.get(key) for key in PHASE_POWER_ENTITIES),
            self._configured_number(CONF_FUSE_LIMIT, DEFAULT_FUSE_LIMIT_A),
            self._configured_number(CONF_PHASE_HEADROOM, DEFAULT_PHASE_HEADROOM_W),
            MAX_BATTERY_POWER_W,
        )
        data["max_charge_power"] = limits[0]
        data["max_discharge_power"] = limits[1]
        return data

    def _configured_number(self, key: str, default: float) -> float:
        """Return a configured numeric setting or its safe default."""
        value = self.entry.options.get(key, self.entry.data.get(key, default))
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def configured_entity_id(self, key: str) -> str | None:
        """Return the configured entity ID for a mapping key."""
        return self.entry.options.get(key, self.entry.data.get(key))
