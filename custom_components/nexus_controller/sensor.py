"""Read-only status entities for Nexus Controller."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ENTITY_LABELS, SENSOR_ENTITIES
from .coordinator import NexusCoordinator


@dataclass(frozen=True, kw_only=True)
class NexusSensorDescription(SensorEntityDescription):
    """Describe a configured input value exposed for diagnostics."""

    config_key: str


SENSORS = tuple(
    NexusSensorDescription(
        key=key,
        name=label,
        native_unit_of_measurement=unit,
        config_key=key,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    for key, (label, unit) in ENTITY_LABELS.items()
    if key in SENSOR_ENTITIES
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create diagnostic sensors for selected input entities."""
    coordinator: NexusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        NexusInputSensor(coordinator, description)
        for description in SENSORS
        if coordinator.configured_entity_id(description.config_key)
    )


class NexusInputSensor(CoordinatorEntity[NexusCoordinator], SensorEntity):
    """Expose the latest numeric value from a configured input entity."""

    entity_description: NexusSensorDescription

    def __init__(
        self,
        coordinator: NexusCoordinator,
        description: NexusSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.config_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="Nexus Controller",
            manufacturer="Nexus Controller",
            model="Solis Modbus entity bridge",
        )

    @property
    def native_value(self) -> float | None:
        """Return the selected input value."""
        return self.coordinator.data.get(self.entity_description.config_key)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update this entity when configured input values change."""
        self.async_write_ha_state()
