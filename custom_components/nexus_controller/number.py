"""Setting and tracker value entities for Nexus Controller."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SET_DIRECTION_DELAY,
    SET_DISCHARGE_OFFSET,
    SET_DISCHARGE_PRICE_ABOVE,
    SET_MANUAL_TIMEOUT,
    SET_MAX_CHARGE_PRICE,
    SET_MIN_CONTROL_POWER,
    SET_MIN_DISCHARGE_SOC,
    SET_MIN_EARNINGS,
    SET_PID_KD,
    SET_PID_KI,
    SET_PID_KP,
    SET_PID_START_ERROR,
    SET_PID_STOP_ERROR,
    SET_RESERVED_ROOM,
    SET_TAX,
    SET_WRITEDOWN_RATE,
    SET_WRITEDOWN_SOC,
    SET_WRITEDOWN_WAIT,
    TRACK_EARNED_TOTAL,
    TRACK_STORED_VALUE,
)
from .coordinator import NexusCoordinator

EUR_KWH = "€/kWh"


@dataclass(frozen=True, kw_only=True)
class NexusNumberDescription(NumberEntityDescription):
    """A runtime setting."""


SETTINGS = (
    NexusNumberDescription(
        key=SET_WRITEDOWN_WAIT,
        translation_key=SET_WRITEDOWN_WAIT,
        native_min_value=0,
        native_max_value=720,
        native_step=1,
        native_unit_of_measurement="h",
    ),
    NexusNumberDescription(
        key=SET_WRITEDOWN_SOC,
        translation_key=SET_WRITEDOWN_SOC,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement="%",
    ),
    NexusNumberDescription(
        key=SET_WRITEDOWN_RATE,
        translation_key=SET_WRITEDOWN_RATE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement="%",
    ),
    NexusNumberDescription(
        key=SET_DIRECTION_DELAY,
        translation_key=SET_DIRECTION_DELAY,
        native_min_value=0,
        native_max_value=600,
        native_step=1,
        native_unit_of_measurement="s",
    ),
    NexusNumberDescription(
        key=SET_MIN_DISCHARGE_SOC,
        translation_key=SET_MIN_DISCHARGE_SOC,
        native_min_value=7,
        native_max_value=99,
        native_step=1,
        native_unit_of_measurement="%",
    ),
    NexusNumberDescription(
        key=SET_MANUAL_TIMEOUT,
        translation_key=SET_MANUAL_TIMEOUT,
        native_min_value=1,
        native_max_value=240,
        native_step=1,
        native_unit_of_measurement="min",
    ),
    NexusNumberDescription(
        key=SET_MIN_CONTROL_POWER,
        translation_key=SET_MIN_CONTROL_POWER,
        native_min_value=0,
        native_max_value=1000,
        native_step=5,
        native_unit_of_measurement="W",
    ),
    NexusNumberDescription(
        key=SET_DISCHARGE_OFFSET,
        translation_key=SET_DISCHARGE_OFFSET,
        native_min_value=0,
        native_max_value=500,
        native_step=5,
        native_unit_of_measurement="W",
    ),
    NexusNumberDescription(
        key=SET_RESERVED_ROOM,
        translation_key=SET_RESERVED_ROOM,
        native_min_value=0,
        native_max_value=5000,
        native_step=50,
        native_unit_of_measurement="W",
    ),
    NexusNumberDescription(
        key=SET_MAX_CHARGE_PRICE,
        translation_key=SET_MAX_CHARGE_PRICE,
        native_min_value=-1,
        native_max_value=2,
        native_step=0.01,
        native_unit_of_measurement=EUR_KWH,
    ),
    NexusNumberDescription(
        key=SET_DISCHARGE_PRICE_ABOVE,
        translation_key=SET_DISCHARGE_PRICE_ABOVE,
        native_min_value=-1,
        native_max_value=2,
        native_step=0.01,
        native_unit_of_measurement=EUR_KWH,
    ),
    NexusNumberDescription(
        key=SET_MIN_EARNINGS,
        translation_key=SET_MIN_EARNINGS,
        native_min_value=0,
        native_max_value=2,
        native_step=0.01,
        native_unit_of_measurement=EUR_KWH,
    ),
    NexusNumberDescription(
        key=SET_TAX,
        translation_key=SET_TAX,
        native_min_value=0,
        native_max_value=1,
        native_step=0.001,
        native_unit_of_measurement=EUR_KWH,
    ),
    NexusNumberDescription(
        key=SET_PID_KP,
        translation_key=SET_PID_KP,
        native_min_value=0,
        native_max_value=2,
        native_step=0.05,
    ),
    NexusNumberDescription(
        key=SET_PID_KI,
        translation_key=SET_PID_KI,
        native_min_value=0,
        native_max_value=1,
        native_step=0.001,
    ),
    NexusNumberDescription(
        key=SET_PID_KD,
        translation_key=SET_PID_KD,
        native_min_value=0,
        native_max_value=10,
        native_step=0.05,
    ),
    NexusNumberDescription(
        key=SET_PID_START_ERROR,
        translation_key=SET_PID_START_ERROR,
        native_min_value=1,
        native_max_value=500,
        native_step=1,
        native_unit_of_measurement="W",
    ),
    NexusNumberDescription(
        key=SET_PID_STOP_ERROR,
        translation_key=SET_PID_STOP_ERROR,
        native_min_value=0,
        native_max_value=499,
        native_step=1,
        native_unit_of_measurement="W",
    ),
)

TRACKED = (
    NexusNumberDescription(
        key=TRACK_EARNED_TOTAL,
        translation_key=TRACK_EARNED_TOTAL,
        native_min_value=-1_000_000,
        native_max_value=1_000_000,
        native_step=0.01,
        native_unit_of_measurement="EUR",
        mode=NumberMode.BOX,
    ),
    NexusNumberDescription(
        key=TRACK_STORED_VALUE,
        translation_key=TRACK_STORED_VALUE,
        native_min_value=-1,
        native_max_value=2,
        native_step=0.001,
        native_unit_of_measurement=EUR_KWH,
        mode=NumberMode.BOX,
    ),
)


def _device_info(coordinator: NexusCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.entry.entry_id)},
        name="Nexus Controller",
        manufacturer="Nexus Controller",
        model="Solis Modbus entity bridge",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the setting and tracker numbers."""
    coordinator: NexusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            *(NexusSettingNumber(coordinator, description) for description in SETTINGS),
            *(NexusTrackerNumber(coordinator, description) for description in TRACKED),
        ]
    )


class NexusSettingNumber(CoordinatorEntity[NexusCoordinator], RestoreNumber):
    """A strategy or PID setting that keeps its value across restarts."""

    entity_description: NexusNumberDescription
    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: NexusCoordinator, description: NexusNumberDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Restore the last value into the coordinator."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and last.native_value is not None:
            self.coordinator.set_setting(self.entity_description.key, last.native_value)
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        """Return the current setting."""
        return float(self.coordinator.settings[self.entity_description.key])

    async def async_set_native_value(self, value: float) -> None:
        """Change the setting."""
        self.coordinator.set_setting(self.entity_description.key, value)
        self.async_write_ha_state()


class NexusTrackerNumber(CoordinatorEntity[NexusCoordinator], NumberEntity):
    """A tracked value that the user may also overwrite (the tracker persists it)."""

    entity_description: NexusNumberDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, coordinator: NexusCoordinator, description: NexusNumberDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_value(self) -> float:
        """Return the tracker value."""
        state = self.coordinator.tracker.state
        if self.entity_description.key == TRACK_EARNED_TOTAL:
            return state.earned_total
        return state.stored_value

    async def async_set_native_value(self, value: float) -> None:
        """Overwrite the tracker value."""
        state = self.coordinator.tracker.state
        if self.entity_description.key == TRACK_EARNED_TOTAL:
            state.earned_total = value
        else:
            state.stored_value = value
        self.async_write_ha_state()
