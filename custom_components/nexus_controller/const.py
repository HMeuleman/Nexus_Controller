"""Constants for Nexus Controller."""

from typing import Final

DOMAIN: Final = "nexus_controller"
NAME: Final = "Nexus Controller"

CONF_GRID_POWER: Final = "grid_power_entity"
CONF_PV_POWER: Final = "pv_power_entity"
CONF_PV_POWER_2: Final = "pv_power_entity_2"
CONF_PV_POWER_3: Final = "pv_power_entity_3"
CONF_BATTERY_SOC: Final = "battery_soc_entity"
CONF_ELECTRICITY_PRICE: Final = "electricity_price_entity"
CONF_CHARGE_CONTROL: Final = "charge_control_entity"
CONF_DISCHARGE_CONTROL: Final = "discharge_control_entity"

PV_POWER_ENTITIES: Final = (CONF_PV_POWER, CONF_PV_POWER_2, CONF_PV_POWER_3)

REQUIRED_ENTITIES: Final = (
    CONF_GRID_POWER,
    CONF_BATTERY_SOC,
    CONF_ELECTRICITY_PRICE,
    CONF_CHARGE_CONTROL,
    CONF_DISCHARGE_CONTROL,
)

SENSOR_ENTITIES: Final = (
    CONF_GRID_POWER,
    CONF_PV_POWER,
    CONF_BATTERY_SOC,
    CONF_ELECTRICITY_PRICE,
)

ENTITY_LABELS: Final = {
    CONF_GRID_POWER: ("Grid power", "W"),
    CONF_PV_POWER: ("Solar production", "W"),
    CONF_BATTERY_SOC: ("Battery state of charge (SOC)", "%"),
    CONF_ELECTRICITY_PRICE: ("Electricity price", "€/kWh"),
}
