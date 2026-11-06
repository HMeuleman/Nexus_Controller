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
CONF_SOLAR_FORECAST: Final = "solar_forecast_entity"
CONF_CHARGE_ENABLE: Final = "charge_enable_entity"
CONF_CHARGE_POWER: Final = "charge_power_entity"
CONF_DISCHARGE_ENABLE: Final = "discharge_enable_entity"
CONF_DISCHARGE_POWER: Final = "discharge_power_entity"
CONF_FUSE_LIMIT: Final = "fuse_limit_amps"

# Legacy config keys, only read to seed the new setting entities.
LEGACY_PHASE_HEADROOM: Final = "phase_headroom_watts"
LEGACY_CHARGE_PRICE_BELOW: Final = "charge_price_below"
LEGACY_DISCHARGE_PRICE_ABOVE: Final = "discharge_price_above"
LEGACY_MIN_MARGIN: Final = "min_margin_eur_kwh"
LEGACY_CYCLE_COST: Final = "cycle_cost_eur_kwh"

# Runtime settings, exposed as number/switch entities instead of config options.
SET_RESERVED_ROOM: Final = "reserved_room_w"
SET_MAX_CHARGE_PRICE: Final = "max_charge_price"
SET_DISCHARGE_PRICE_ABOVE: Final = "discharge_price_above"
SET_MIN_EARNINGS: Final = "min_earnings_per_kwh"
SET_TAX: Final = "tax_per_kwh"
SET_PID_KP: Final = "pid_kp"
SET_PID_KI: Final = "pid_ki"
SET_PID_KD: Final = "pid_kd"
SET_PID_START_ERROR: Final = "pid_start_error_w"
SET_PID_STOP_ERROR: Final = "pid_stop_error_w"
SET_MIN_CONTROL_POWER: Final = "min_control_power_w"
SET_MIN_DISCHARGE_SOC: Final = "min_discharge_soc"
SET_DIRECTION_DELAY: Final = "direction_delay_s"
SET_SOLAR_BLOCK: Final = "solar_block_discharge_w"
SET_WRITEDOWN_WAIT: Final = "writedown_wait_h"
SET_WRITEDOWN_SOC: Final = "writedown_min_soc"
SET_WRITEDOWN_RATE: Final = "writedown_percent_per_day"
SET_MANUAL_TIMEOUT: Final = "manual_timeout_min"
MANUAL_CHARGE: Final = "manual_charge"
MANUAL_DISCHARGE: Final = "manual_discharge"
SET_DISCHARGE_OFFSET: Final = "discharge_offset_w"
SET_EXPORT_INCLUDES_TAX: Final = "export_includes_tax"
SET_CONTROL_ENABLED: Final = "control_enabled"

# The two writable tracker values, also exposed as numbers.
TRACK_EARNED_TOTAL: Final = "earned_total"
TRACK_STORED_VALUE: Final = "stored_energy_value"

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
# Defaults follow the offline simulation of May-October data; see sim/closed_loop.py.
# Min earnings is the former margin (0.12) plus wear (0.08).
SETTING_DEFAULTS: Final[dict[str, float | bool]] = {
    SET_RESERVED_ROOM: 1_000.0,
    SET_MAX_CHARGE_PRICE: 0.15,
    SET_DISCHARGE_PRICE_ABOVE: 0.0,
    SET_MIN_EARNINGS: 0.20,
    SET_TAX: 0.11,
    # Conservative until tuned on the real inverter: the offline sim assumed an ideal plant.
    SET_PID_KP: 0.5,
    SET_PID_KI: 0.0,
    SET_PID_KD: 0.0,
    SET_PID_START_ERROR: 15.0,
    SET_PID_STOP_ERROR: 8.0,
    SET_MIN_CONTROL_POWER: 100.0,
    SET_DISCHARGE_OFFSET: 50.0,
    SET_MIN_DISCHARGE_SOC: 10.0,
    SET_DIRECTION_DELAY: 60.0,
    SET_SOLAR_BLOCK: 10_000.0,
    SET_WRITEDOWN_WAIT: 24.0,
    SET_WRITEDOWN_SOC: 50.0,
    SET_WRITEDOWN_RATE: 33.0,
    SET_MANUAL_TIMEOUT: 30.0,
    SET_EXPORT_INCLUDES_TAX: False,
    SET_CONTROL_ENABLED: False,
}
DEFAULT_STORED_VALUE: Final = 0.10
BATTERY_CAPACITY_KWH: Final = 20.8
ROUND_TRIP_EFFICIENCY: Final = 0.92
PID_INTEGRAL_LIMIT_WS: Final = 5_000.0
PID_FILTER_ALPHA: Final = 1.0
DEFAULT_SAMPLE_INTERVAL_S: Final = 10.0
MIN_CHARGE_POWER_W: Final = 500.0
STORAGE_VERSION: Final = 1
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
    "price_tax_free",
    "tax_used",
    "cycles_today",
    "cycles_total",
    "earned_today",
    "sample_interval",
    "discharge_threshold",
    "avg_daytime_load",
    "solar_surplus_forecast",
    "control_mode",
    "control_power",
    "control_status",
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
    "price_tax_free": ("price_tax_free", "€/kWh"),
    "tax_used": ("tax_used", "€/kWh"),
    "cycles_today": ("cycles_today", None),
    "cycles_total": ("cycles_total", None),
    "earned_today": ("earned_today", "EUR"),
    "sample_interval": ("sample_interval", "s"),
    "discharge_threshold": ("discharge_threshold", "€/kWh"),
    "avg_daytime_load": ("avg_daytime_load", "W"),
    "solar_surplus_forecast": ("solar_surplus_forecast", "kWh"),
    "control_mode": ("control_mode", None),
    "control_power": ("control_power", "W"),
    "control_status": ("control_status", None),
}

ENTITY_PRECISION: Final = {
    "price_tax_free": 4,
    "tax_used": 4,
    "cycles_today": 2,
    "cycles_total": 2,
    "earned_today": 2,
    "sample_interval": 1,
    "discharge_threshold": 3,
    "avg_daytime_load": 0,
    "solar_surplus_forecast": 1,
}
