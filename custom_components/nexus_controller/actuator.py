"""Write battery commands to the configured Solis control entities (safety-critical)."""

from __future__ import annotations

import logging
import math
import time

from homeassistant.core import HomeAssistant

from .dispatch import DispatchMode

_LOGGER = logging.getLogger(__name__)

POWER_CHANGE_DEADBAND_W = 25.0
MIN_WRITE_INTERVAL_S = 2.0
# The Solis falls back to 0 when the power is not rewritten for a few minutes.
KEEPALIVE_S = 60.0


class ActuatorError(Exception):
    """A command could not be written safely."""


class BatteryActuator:
    """Maps a (mode, power) request onto the enable switches and power numbers.

    Mode changes go through a fixed sequence: both modes off, target power set, then the
    target mode on. Idle simply turns both modes off, which returns the inverter to its
    own behaviour.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        charge_enable: str,
        charge_power: str,
        discharge_enable: str,
        discharge_power: str,
    ) -> None:
        self._hass = hass
        self._enable = {"charge": charge_enable, "discharge": discharge_enable}
        self._power = {"charge": charge_power, "discharge": discharge_power}
        self._mode: DispatchMode | None = None  # None means unknown: force a full sequence
        self._power_w = 0.0
        self._last_write = 0.0

    async def async_apply(
        self, mode: DispatchMode, power_w: float, *, min_interval_s: float
    ) -> str:
        """Apply the request and return a short status string."""
        if mode == "idle" or not math.isfinite(power_w) or power_w <= 0:
            return await self._async_idle()

        now = time.monotonic()
        if mode == self._mode:
            if abs(power_w - self._power_w) < POWER_CHANGE_DEADBAND_W:
                if now - self._last_write >= KEEPALIVE_S:
                    await self._async_set_power(mode, self._power_w)
                    self._last_write = now
                    return "keepalive"
                return "holding"
            if now - self._last_write < max(min_interval_s, MIN_WRITE_INTERVAL_S):
                return "rate_limited"
            self._power_w = await self._async_set_power(mode, power_w)
            self._last_write = now
            return "power_updated"

        other = "discharge" if mode == "charge" else "charge"
        await self._async_switch(other, False)
        await self._async_switch(mode, False)
        self._power_w = await self._async_set_power(mode, power_w)
        await self._async_switch(mode, True)
        self._mode = mode
        self._last_write = now
        return "mode_started"

    async def async_release(self) -> str:
        """Stop forced charging/discharging; safe to call repeatedly."""
        self._mode = None
        return await self._async_idle(force=True)

    async def _async_idle(self, *, force: bool = False) -> str:
        if self._mode == "idle" and not force:
            return "idle"
        for mode in ("charge", "discharge"):
            await self._async_switch(mode, False)
        self._mode = "idle"
        self._power_w = 0.0
        self._last_write = time.monotonic()
        return "idle"

    async def _async_switch(self, mode: str, on: bool) -> None:
        await self._call(
            "switch", "turn_on" if on else "turn_off", {"entity_id": self._enable[mode]}
        )

    async def _async_set_power(self, mode: str, power_w: float) -> float:
        """Write the power in the entity's own unit, within its limits; return watts written."""
        entity_id = self._power[mode]
        state = self._hass.states.get(entity_id)
        if state is None:
            raise ActuatorError(f"{entity_id} is not available")
        unit = (state.attributes.get("unit_of_measurement") or "").casefold()
        factor = {"w": 1.0, "kw": 1000.0}.get(unit)
        if factor is None:
            raise ActuatorError(f"{entity_id} has unsupported unit {unit!r}")
        value = power_w / factor
        step = state.attributes.get("step")
        if isinstance(step, (int, float)) and step > 0:
            value = round(value / step) * step
        minimum = state.attributes.get("min")
        maximum = state.attributes.get("max")
        if isinstance(maximum, (int, float)):
            value = min(value, maximum)
        if isinstance(minimum, (int, float)):
            value = max(value, minimum)
        await self._call("number", "set_value", {"entity_id": entity_id, "value": value})
        return value * factor

    async def _call(self, domain: str, service: str, data: dict) -> None:
        try:
            await self._hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # service layer raises many types; all mean "not written"
            _LOGGER.error("Writing %s.%s %s failed: %s", domain, service, data, err)
            raise ActuatorError(str(err)) from err
