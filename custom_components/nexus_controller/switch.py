"""Switches for Nexus Controller."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SET_CONTROL_ENABLED, SET_EXPORT_INCLUDES_TAX
from .coordinator import NexusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the control and export-tax switches."""
    coordinator: NexusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NexusControlSwitch(coordinator), NexusExportTaxSwitch(coordinator)])


class _NexusSwitch(CoordinatorEntity[NexusCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _key: str

    def __init__(self, coordinator: NexusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = self._key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{self._key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="Nexus Controller",
            manufacturer="Nexus Controller",
            model="Solis Modbus entity bridge",
        )

    @property
    def is_on(self) -> bool:
        """Return the current setting."""
        return bool(self.coordinator.settings[self._key])


class NexusControlSwitch(_NexusSwitch):
    """Lets the integration drive the battery; always off after a restart for safety."""

    _key = SET_CONTROL_ENABLED
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start controlling the battery."""
        await self.coordinator.async_set_control(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Release the battery back to the inverter."""
        await self.coordinator.async_set_control(False)


class NexusExportTaxSwitch(_NexusSwitch, RestoreEntity):
    """Whether exported energy is paid including taxes (2026) or without (2027 onwards)."""

    _key = SET_EXPORT_INCLUDES_TAX
    _attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        """Restore the last choice."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self.coordinator.set_setting(self._key, last.state == "on")
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Value exported energy including taxes."""
        self.coordinator.set_setting(self._key, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Value exported energy without taxes."""
        self.coordinator.set_setting(self._key, False)
        self.async_write_ha_state()
