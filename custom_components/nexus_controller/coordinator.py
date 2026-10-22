"""Coordinator for configured Nexus sensors."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, REQUIRED_ENTITIES
from .utils import parse_numeric_state

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
            data[key] = parse_numeric_state(state.state if state is not None else None)
        return data

    def configured_entity_id(self, key: str) -> str | None:
        """Return the configured entity ID for a mapping key."""
        return self.entry.options.get(key, self.entry.data.get(key))
