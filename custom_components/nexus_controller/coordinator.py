"""Coordinator for configured Nexus sensors."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_CHARGE_PRICE_BELOW,
    CONF_CYCLE_COST,
    CONF_DISCHARGE_PRICE_ABOVE,
    CONF_ELECTRICITY_PRICE,
    CONF_EXPORT_TAX,
    CONF_FUSE_LIMIT,
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
    EXPORT_RESERVE_SOC_PCT,
    INPUT_SENSOR_ENTITIES,
    MAX_BATTERY_POWER_W,
    MIN_SOC_PCT,
    PHASE_POWER_ENTITIES,
    PV_POWER_ENTITIES,
    ROUND_TRIP_EFFICIENCY,
)
from .dispatch import DispatchSettings
from .net_charge import ExportSettings, NetChargeSettings
from .shadow import ShadowSettings, infer_slot_hours, propose_shadow_action
from .utils import (
    calculate_phase_power_limits,
    parse_numeric_state,
    parse_price_forecast,
    sum_power_states,
    sum_pv_power_states,
)

_LOGGER = logging.getLogger(__name__)


class NexusCoordinator(DataUpdateCoordinator[dict[str, float | str | None]]):
    """Read configured HA entities; this version never commands hardware."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=10),
        )
        self.entry = entry
        self.price_forecast: list[tuple[datetime, float]] = []

    async def _async_update_data(self) -> dict[str, float | str | None]:
        """Read selected entity values and represent unavailable states as None."""
        data: dict[str, float | str | None] = {}
        for key in INPUT_SENSOR_ENTITIES:
            entity_id = self.entry.options.get(key, self.entry.data.get(key))
            state = self.hass.states.get(entity_id) if entity_id else None
            if key in PHASE_POWER_ENTITIES:
                data[key] = sum_power_states(
                    [
                        (
                            state.state if state else None,
                            state.attributes.get("unit_of_measurement") if state else None,
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
        data[PV_POWER_ENTITIES[0]] = sum_pv_power_states(pv_states)
        limits = calculate_phase_power_limits(
            tuple(data.get(key) for key in PHASE_POWER_ENTITIES),
            self._configured_number(CONF_FUSE_LIMIT, DEFAULT_FUSE_LIMIT_A),
            self._configured_number(CONF_PHASE_HEADROOM, DEFAULT_PHASE_HEADROOM_W),
            MAX_BATTERY_POWER_W,
        )
        data["max_charge_power"] = limits[0]
        data["max_discharge_power"] = limits[1]

        # Without a dedicated forecast sensor, the price sensor's own attribute is used.
        forecast_entity_id = self.configured_entity_id(
            CONF_PRICE_FORECAST
        ) or self.configured_entity_id(CONF_ELECTRICITY_PRICE)
        forecast_state = self.hass.states.get(forecast_entity_id) if forecast_entity_id else None
        self.price_forecast = parse_price_forecast(
            forecast_state.attributes.get("forecast") if forecast_state is not None else None
        )
        now = dt_util.utcnow()
        upcoming = [price for start, price in self.price_forecast if start > now]
        data["price_forecast_points"] = float(len(upcoming))
        data["forecast_lowest_price"] = min(upcoming) if upcoming else None
        data["forecast_highest_price"] = max(upcoming) if upcoming else None

        self._add_shadow_proposal(data, now)
        return data

    def _add_shadow_proposal(self, data: dict[str, float | str | None], now: datetime) -> None:
        """Add the read-only proposal; it needs the measured battery power."""
        battery_entity_id = self.configured_entity_id(CONF_BATTERY_POWER)
        if not battery_entity_id:
            data["shadow_mode"] = "idle"
            data["shadow_power"] = None
            data["shadow_reason"] = "battery_power_not_configured"
            return

        battery_state = self.hass.states.get(battery_entity_id)
        battery_power = sum_power_states(
            [
                (
                    battery_state.state if battery_state is not None else None,
                    battery_state.attributes.get("unit_of_measurement")
                    if battery_state is not None
                    else None,
                )
            ]
        )
        upcoming_points = [(start, price) for start, price in self.price_forecast if start > now]
        horizon_end = now + timedelta(hours=24)
        plan = propose_shadow_action(
            phase_imports_w=tuple(data.get(key) for key in PHASE_POWER_ENTITIES),  # type: ignore[arg-type]
            battery_power_w=battery_power,
            soc_pct=data.get(CONF_BATTERY_SOC),  # type: ignore[arg-type]
            price=data.get(CONF_ELECTRICITY_PRICE),  # type: ignore[arg-type]
            pv_power_w=data.get(PV_POWER_ENTITIES[0]),  # type: ignore[arg-type]
            upcoming_prices=[price for start, price in upcoming_points if start <= horizon_end],
            slot_hours=infer_slot_hours([start for start, _ in upcoming_points]),
            fuse_limit_amps=self._configured_number(CONF_FUSE_LIMIT, DEFAULT_FUSE_LIMIT_A),
            phase_headroom_w=self._configured_number(CONF_PHASE_HEADROOM, DEFAULT_PHASE_HEADROOM_W),
            settings=self._shadow_settings(),
        )
        data["shadow_mode"] = plan.mode
        data["shadow_power"] = plan.power_w
        data["shadow_reason"] = plan.reason

    def _shadow_settings(self) -> ShadowSettings:
        """Build strategy settings from the configuration or its defaults."""
        cycle_cost = self._configured_number(CONF_CYCLE_COST, DEFAULT_CYCLE_COST)
        margin = self._configured_number(CONF_MIN_MARGIN, DEFAULT_MIN_MARGIN)
        return ShadowSettings(
            dispatch=DispatchSettings(
                charge_price_below=self._configured_number(
                    CONF_CHARGE_PRICE_BELOW, DEFAULT_CHARGE_PRICE_BELOW
                ),
                discharge_price_above=self._configured_number(
                    CONF_DISCHARGE_PRICE_ABOVE, DEFAULT_DISCHARGE_PRICE_ABOVE
                ),
                min_soc_pct=MIN_SOC_PCT,
                max_soc_pct=100.0,
            ),
            net_charge=NetChargeSettings(
                margin, ROUND_TRIP_EFFICIENCY, MAX_BATTERY_POWER_W, cycle_cost
            ),
            export=ExportSettings(
                margin,
                ROUND_TRIP_EFFICIENCY,
                self._configured_number(CONF_EXPORT_TAX, DEFAULT_EXPORT_TAX),
                cycle_cost,
                EXPORT_RESERVE_SOC_PCT,
                MAX_BATTERY_POWER_W,
            ),
            capacity_kwh=self._configured_number(
                CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH
            ),
        )

    def _configured_number(self, key: str, default: float) -> float:
        """Return a configured numeric setting or its safe default."""
        value = self.entry.options.get(key, self.entry.data.get(key, default))
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def configured_entity_id(self, key: str) -> str | None:
        """Return the configured entity ID for a mapping key."""
        return self.entry.options.get(key, self.entry.data.get(key))
