"""Coordinator for configured Nexus sensors."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_GRID_POWER, DOMAIN, PV_POWER_ENTITIES, REQUIRED_ENTITIES
from .utils import parse_numeric_state, sum_power_states

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
        for key in REQUIRED_ENTITIES:
            entity_id = self.entry.options.get(key, self.entry.data.get(key))
            state = self.hass.states.get(entity_id) if entity_id else None
            if key == CONF_GRID_POWER:
                data[key] = sum_power_states(
                    [
                        (
                            state.state if state is not None else None,
                            state.attributes.get("unit_of_measurement")
                            if state is not None
                            else None,
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
        data[PV_POWER_ENTITIES[0]] = sum_power_states(pv_states)
        return data

    def configured_entity_id(self, key: str) -> str | None:
        """Return the configured entity ID for a mapping key."""
        if key in PV_POWER_ENTITIES and self.entry.options:
            return self.entry.options.get(key)
        return self.entry.options.get(key, self.entry.data.get(key))
