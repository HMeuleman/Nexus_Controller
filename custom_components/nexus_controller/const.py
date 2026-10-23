"""Constants for Nexus Controller."""

from typing import Final

DOMAIN: Final = "nexus_controller"
NAME: Final = "Nexus Controller"

CONF_GRID_POWER_L1: Final = "grid_power_l1_entity"
CONF_GRID_POWER_L2: Final = "grid_power_l2_entity"
CONF_GRID_POWER_L3: Final = "grid_power_l3_entity"
CONF_PV_POWER: Final = "pv_power_entity"
CONF_PV_POWER_2: Final = "pv_power_entity_2"
CONF_PV_POWER_3: Final = "pv_power_entity_3"
CONF_BATTERY_SOC: Final = "battery_soc_entity"
CONF_ELECTRICITY_PRICE: Final = "electricity_price_entity"
CONF_PRICE_FORECAST: Final = "price_forecast_entity"
CONF_BATTERY_POWER: Final = "battery_power_entity"
CONF_CHARGE_PRICE_BELOW: Final = "charge_price_below"
CONF_DISCHARGE_PRICE_ABOVE: Final = "discharge_price_above"
CONF_MIN_MARGIN: Final = "min_margin_eur_kwh"
CONF_CYCLE_COST: Final = "cycle_cost_eur_kwh"
CONF_EXPORT_TAX: Final = "export_tax_eur_kwh"
CONF_BATTERY_CAPACITY: Final = "battery_capacity_kwh"
CONF_CHARGE_ENABLE: Final = "charge_enable_entity"
CONF_CHARGE_POWER: Final = "charge_power_entity"
CONF_DISCHARGE_ENABLE: Final = "discharge_enable_entity"
CONF_DISCHARGE_POWER: Final = "discharge_power_entity"
CONF_FUSE_LIMIT: Final = "fuse_limit_amps"
CONF_PHASE_HEADROOM: Final = "phase_headroom_watts"

PV_POWER_ENTITIES: Final = (CONF_PV_POWER, CONF_PV_POWER_2, CONF_PV_POWER_3)
PHASE_POWER_ENTITIES: Final = (CONF_GRID_POWER_L1, CONF_GRID_POWER_L2, CONF_GRID_POWER_L3)
CONTROL_ENTITIES: Final = (
    CONF_CHARGE_ENABLE,
    CONF_CHARGE_POWER,
    CONF_DISCHARGE_ENABLE,
    CONF_DISCHARGE_POWER,
)
MAX_BATTERY_POWER_W: Final = 10_000
DEFAULT_FUSE_LIMIT_A: Final = 25
DEFAULT_PHASE_HEADROOM_W: Final = 1_000
# Defaults follow the offline simulation of May-October data; see sim/closed_loop.py.
DEFAULT_CHARGE_PRICE_BELOW: Final = 0.15
DEFAULT_DISCHARGE_PRICE_ABOVE: Final = 0.25
DEFAULT_MIN_MARGIN: Final = 0.12
DEFAULT_CYCLE_COST: Final = 0.08
DEFAULT_EXPORT_TAX: Final = 0.09
DEFAULT_BATTERY_CAPACITY_KWH: Final = 20.8
ROUND_TRIP_EFFICIENCY: Final = 0.92
EXPORT_RESERVE_SOC_PCT: Final = 30.0
MIN_SOC_PCT: Final = 7.0

INPUT_SENSOR_ENTITIES: Final = (
    *PHASE_POWER_ENTITIES,
    CONF_BATTERY_SOC,
    CONF_ELECTRICITY_PRICE,
)

SENSOR_ENTITIES: Final = (
    *PHASE_POWER_ENTITIES,
    CONF_PV_POWER,
    CONF_BATTERY_SOC,
    CONF_ELECTRICITY_PRICE,
    "max_charge_power",
    "max_discharge_power",
    "price_forecast_points",
    "forecast_lowest_price",
    "forecast_highest_price",
)

ENTITY_LABELS: Final = {
    CONF_GRID_POWER_L1: ("grid_power_l1", "W"),
    CONF_GRID_POWER_L2: ("grid_power_l2", "W"),
    CONF_GRID_POWER_L3: ("grid_power_l3", "W"),
    CONF_PV_POWER: ("pv_power", "W"),
    CONF_BATTERY_SOC: ("battery_soc", "%"),
    CONF_ELECTRICITY_PRICE: ("electricity_price", "€/kWh"),
    "max_charge_power": ("max_charge_power", "W"),
    "max_discharge_power": ("max_discharge_power", "W"),
    "price_forecast_points": ("price_forecast_points", None),
    "forecast_lowest_price": ("forecast_lowest_price", "€/kWh"),
    "forecast_highest_price": ("forecast_highest_price", "€/kWh"),
    "shadow_mode": ("shadow_mode", None),
    "shadow_power": ("shadow_power", "W"),
    "shadow_reason": ("shadow_reason", None),
}
