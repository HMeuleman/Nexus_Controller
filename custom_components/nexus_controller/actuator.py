"""Write battery commands to the configured Solis control entities (safety-critical)."""

from __future__ import annotations

import logging
import math
import time

from homeassistant.core import HomeAssistant

from .dispatch import DispatchMode

_LOGGER = logging.getLogger(__name__)

# Must stay well below the PID step (kp x error), or small corrections never get written.
POWER_CHANGE_DEADBAND_W = 5.0
MIN_WRITE_INTERVAL_S = 2.0
# The Solis falls back to 0 when the power is not rewritten for a few minutes.
KEEPALIVE_S = 60.0
# The enable switch may lag behind a write; do not judge it before this has passed.
SETTLE_S = 15.0


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
        self._last_enable = 0.0

    async def async_apply(
        self, mode: DispatchMode, power_w: float, *, min_interval_s: float
    ) -> str:
        """Apply the request and return a short status string."""
        if mode == "idle" or not math.isfinite(power_w) or power_w <= 0:
            return await self._async_idle()

        now = time.monotonic()
        if mode == self._mode:
            enable = self._hass.states.get(self._enable[mode])
            if enable is not None and enable.state != "on" and now - self._last_enable > SETTLE_S:
                # The inverter dropped the mode by itself: start it again.
                await self._async_start(mode, power_w, other_off=False)
                return "restarted"
            if now - self._last_enable >= KEEPALIVE_S:
                # Toggle off/on so the inverter sees a fresh enable, not an unchanged value.
                await self._async_start(mode, power_w, other_off=False)
                return "keepalive"
            if abs(power_w - self._power_w) < POWER_CHANGE_DEADBAND_W:
                return "holding"
            if now - self._last_write < max(min_interval_s, MIN_WRITE_INTERVAL_S):
                return "rate_limited"
            self._power_w = await self._async_set_power(mode, power_w)
            self._last_write = now
            return "power_updated"

        await self._async_start(mode, power_w, other_off=True)
        return "mode_started"

    async def _async_start(self, mode: str, power_w: float, *, other_off: bool) -> None:
        """Both modes off (or just this one), set the power, then enable the mode."""
        if other_off:
            await self._async_switch("discharge" if mode == "charge" else "charge", False)
        await self._async_switch(mode, False)
        self._power_w = await self._async_set_power(mode, power_w)
        await self._async_switch(mode, True)
        self._mode = mode  # type: ignore[assignment]
        self._last_write = self._last_enable = time.monotonic()

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
