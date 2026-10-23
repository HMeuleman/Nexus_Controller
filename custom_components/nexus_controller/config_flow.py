"""Config flow for Nexus Controller."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_CHARGE_ENABLE,
    CONF_CHARGE_POWER,
    CONF_CHARGE_PRICE_BELOW,
    CONF_CYCLE_COST,
    CONF_DISCHARGE_ENABLE,
    CONF_DISCHARGE_POWER,
    CONF_DISCHARGE_PRICE_ABOVE,
    CONF_ELECTRICITY_PRICE,
    CONF_EXPORT_TAX,
    CONF_FUSE_LIMIT,
    CONF_GRID_POWER_L1,
    CONF_GRID_POWER_L2,
    CONF_GRID_POWER_L3,
    CONF_MIN_MARGIN,
    CONF_PHASE_HEADROOM,
    CONF_PRICE_FORECAST,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_CHARGE_PRICE_BELOW,
    DEFAULT_CYCLE_COST,
    DEFAULT_DISCHARGE_PRICE_ABOVE,
    DEFAULT_EXPORT_TAX,
    DEFAULT_FUSE_LIMIT_A,
    DEFAULT_MIN_MARGIN,
    DEFAULT_PHASE_HEADROOM_W,
    DOMAIN,
    NAME,
    PHASE_POWER_ENTITIES,
    PV_POWER_ENTITIES,
)
from .utils import unique_entity_id_with_suffix

CONTROL_MATCHES = {
    CONF_CHARGE_ENABLE: ("switch", "rc_force_battery_charge"),
    CONF_DISCHARGE_ENABLE: ("switch", "rc_force_battery_discharge"),
    CONF_CHARGE_POWER: ("number", "rc_force_battery_charge_power"),
    CONF_DISCHARGE_POWER: ("number", "rc_force_battery_discharge_power"),
}


def _entity_selector(domain: str | list[str]) -> selector.EntitySelector:
    """Build an entity selector for a specified entity domain."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain, multiple=False))


def _control_defaults(hass) -> dict[str, str]:
    """Find unique existing Solis control entities by their stable name suffix."""
    registry = er.async_get(hass)
    entity_ids = [entry.entity_id for entry in registry.entities.values()]
    matches = {}
    for key, (domain, suffix) in CONTROL_MATCHES.items():
        match = unique_entity_id_with_suffix(
            [entity_id for entity_id in entity_ids if entity_id.startswith(f"{domain}.")],
            suffix,
        )
        if match:
            matches[key] = match
    return matches


def _config_schema(defaults: dict | None = None) -> vol.Schema:
    """Build entity mapping and phase safety settings form schema."""
    defaults = defaults or {}
    schema: dict = {
        vol.Required(
            CONF_GRID_POWER_L1, default=defaults.get(CONF_GRID_POWER_L1)
        ): _entity_selector("sensor"),
        vol.Required(
            CONF_GRID_POWER_L2, default=defaults.get(CONF_GRID_POWER_L2)
        ): _entity_selector("sensor"),
        vol.Required(
            CONF_GRID_POWER_L3, default=defaults.get(CONF_GRID_POWER_L3)
        ): _entity_selector("sensor"),
        vol.Required(CONF_BATTERY_SOC, default=defaults.get(CONF_BATTERY_SOC)): _entity_selector(
            "sensor"
        ),
        vol.Required(
            CONF_ELECTRICITY_PRICE, default=defaults.get(CONF_ELECTRICITY_PRICE)
        ): _entity_selector("sensor"),
        vol.Required(
            CONF_CHARGE_ENABLE, default=defaults.get(CONF_CHARGE_ENABLE)
        ): _entity_selector("switch"),
        vol.Required(CONF_CHARGE_POWER, default=defaults.get(CONF_CHARGE_POWER)): _entity_selector(
            "number"
        ),
        vol.Required(
            CONF_DISCHARGE_ENABLE, default=defaults.get(CONF_DISCHARGE_ENABLE)
        ): _entity_selector("switch"),
        vol.Required(
            CONF_DISCHARGE_POWER, default=defaults.get(CONF_DISCHARGE_POWER)
        ): _entity_selector("number"),
        vol.Required(
            CONF_FUSE_LIMIT, default=defaults.get(CONF_FUSE_LIMIT, DEFAULT_FUSE_LIMIT_A)
        ): vol.All(vol.Coerce(float), vol.Range(min=1, max=63)),
        vol.Required(
            CONF_PHASE_HEADROOM,
            default=defaults.get(CONF_PHASE_HEADROOM, DEFAULT_PHASE_HEADROOM_W),
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5000)),
    }
    strategy_defaults = (
        (CONF_CHARGE_PRICE_BELOW, DEFAULT_CHARGE_PRICE_BELOW, -1.0, 2.0),
        (CONF_DISCHARGE_PRICE_ABOVE, DEFAULT_DISCHARGE_PRICE_ABOVE, -1.0, 2.0),
        (CONF_MIN_MARGIN, DEFAULT_MIN_MARGIN, 0.0, 1.0),
        (CONF_CYCLE_COST, DEFAULT_CYCLE_COST, 0.0, 1.0),
        (CONF_EXPORT_TAX, DEFAULT_EXPORT_TAX, 0.0, 1.0),
        (CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH, 1.0, 100.0),
    )
    for key, default, minimum, maximum in strategy_defaults:
        schema[vol.Optional(key, default=defaults.get(key, default))] = vol.All(
            vol.Coerce(float), vol.Range(min=minimum, max=maximum)
        )
    for key in (*PV_POWER_ENTITIES, CONF_PRICE_FORECAST, CONF_BATTERY_POWER):
        if key in defaults and defaults[key]:
            schema[vol.Optional(key, default=defaults[key])] = _entity_selector("sensor")
        else:
            schema[vol.Optional(key)] = _entity_selector("sensor")
    return vol.Schema(schema)


def _has_duplicate_entities(user_input: dict) -> bool:
    """Reject duplicate phase or PV sensor assignments."""
    keys = (*PHASE_POWER_ENTITIES, *PV_POWER_ENTITIES)
    selections = [user_input[key] for key in keys if user_input.get(key)]
    return len(selections) != len(set(selections))


def _validation_error(user_input: dict) -> str | None:
    """Return an error key for invalid input, or None when it is acceptable."""
    if _has_duplicate_entities(user_input):
        return "duplicate_entities"
    charge_below = user_input.get(CONF_CHARGE_PRICE_BELOW, DEFAULT_CHARGE_PRICE_BELOW)
    discharge_above = user_input.get(CONF_DISCHARGE_PRICE_ABOVE, DEFAULT_DISCHARGE_PRICE_ABOVE)
    if charge_below >= discharge_above:
        return "invalid_price_band"
    return None


class NexusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup for Nexus Controller."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        """Select phase sensors, battery inputs, controls, and safety settings."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        if user_input is not None:
            if error := _validation_error(user_input):
                return self.async_show_form(
                    step_id="user",
                    data_schema=_config_schema(user_input),
                    errors={"base": error},
                )
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=NAME, data=user_input)

        defaults = _control_defaults(self.hass)
        return self.async_show_form(step_id="user", data_schema=_config_schema(defaults))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Return the options flow handler."""
        return NexusOptionsFlow()


class NexusOptionsFlow(config_entries.OptionsFlow):
    """Allow entity mappings and phase safety settings to be changed."""

    async def async_step_init(self, user_input: dict | None = None):
        """Update selected entities and settings."""
        if user_input is not None:
            if error := _validation_error(user_input):
                return self.async_show_form(
                    step_id="init",
                    data_schema=_config_schema(user_input),
                    errors={"base": error},
                )
            for key in (*PV_POWER_ENTITIES, CONF_PRICE_FORECAST, CONF_BATTERY_POWER):
                user_input.setdefault(key, None)
            return self.async_create_entry(title="", data=user_input)

        values = {**self.config_entry.data, **self.config_entry.options}
        for key, value in _control_defaults(self.hass).items():
            values.setdefault(key, value)
        return self.async_show_form(step_id="init", data_schema=_config_schema(values))
