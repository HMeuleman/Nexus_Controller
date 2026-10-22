"""Config flow for Nexus Controller."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_SOC,
    CONF_CHARGE_CONTROL,
    CONF_DISCHARGE_CONTROL,
    CONF_ELECTRICITY_PRICE,
    CONF_GRID_POWER,
    CONF_PV_POWER,
    CONF_PV_POWER_2,
    CONF_PV_POWER_3,
    DOMAIN,
    NAME,
)


def _entity_selector(domain: str | list[str]) -> selector.EntitySelector:
    """Build an entity selector for a specified entity domain."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain, multiple=False))


def _config_schema(defaults: dict | None = None) -> vol.Schema:
    """Build the entity mapping form schema."""
    defaults = defaults or {}
    schema: dict = {
            vol.Required(CONF_GRID_POWER, default=defaults.get(CONF_GRID_POWER)): _entity_selector(
                "sensor"
            ),
            vol.Required(
                CONF_BATTERY_SOC, default=defaults.get(CONF_BATTERY_SOC)
            ): _entity_selector("sensor"),
            vol.Required(
                CONF_ELECTRICITY_PRICE, default=defaults.get(CONF_ELECTRICITY_PRICE)
            ): _entity_selector("sensor"),
            vol.Required(
                CONF_CHARGE_CONTROL, default=defaults.get(CONF_CHARGE_CONTROL)
            ): _entity_selector(["number", "switch", "select"]),
            vol.Required(
                CONF_DISCHARGE_CONTROL, default=defaults.get(CONF_DISCHARGE_CONTROL)
            ): _entity_selector(["number", "switch", "select"]),
    }
    for key in (CONF_PV_POWER, CONF_PV_POWER_2, CONF_PV_POWER_3):
        if key in defaults:
            schema[vol.Optional(key, default=defaults[key])] = _entity_selector("sensor")
        else:
            schema[vol.Optional(key)] = _entity_selector("sensor")
    return vol.Schema(schema)


def _has_duplicate_pv_entities(user_input: dict) -> bool:
    """Return whether a PV entity is selected in more than one slot."""
    selections = [
        user_input[key]
        for key in (CONF_PV_POWER, CONF_PV_POWER_2, CONF_PV_POWER_3)
        if user_input.get(key)
    ]
    return len(selections) != len(set(selections))


class NexusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup for Nexus Controller."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        """Select required sensors and Solis control entities."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        if user_input is not None:
            if _has_duplicate_pv_entities(user_input):
                return self.async_show_form(
                    step_id="user",
                    data_schema=_config_schema(user_input),
                    errors={"base": "duplicate_pv_entities"},
                )
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=NAME, data=user_input)
        return self.async_show_form(step_id="user", data_schema=_config_schema())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Return the options flow handler."""
        return NexusOptionsFlow(config_entry)


class NexusOptionsFlow(config_entries.OptionsFlow):
    """Allow entity mappings to be changed after setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None):
        """Update selected entities."""
        if user_input is not None:
            if _has_duplicate_pv_entities(user_input):
                return self.async_show_form(
                    step_id="init",
                    data_schema=_config_schema(user_input),
                    errors={"base": "duplicate_pv_entities"},
                )
            return self.async_create_entry(title="", data=user_input)
        values = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_config_schema(values))
