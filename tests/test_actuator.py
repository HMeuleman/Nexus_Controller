"""Tests for the battery actuator write sequence and keepalive."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.nexus_controller import actuator as actuator_module
from custom_components.nexus_controller.actuator import ActuatorError, BatteryActuator


def _actuator():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    attrs = {"unit_of_measurement": "W", "min": 0, "max": 10000, "step": 10}
    hass.states.get.return_value = SimpleNamespace(state="on", attributes=attrs)
    return BatteryActuator(hass, "switch.c", "number.cp", "switch.d", "number.dp"), hass


def _calls(hass):
    return [(c.args[0], c.args[1], c.args[2]) for c in hass.services.async_call.await_args_list]


def test_mode_start_sets_power_before_enabling():
    actuator, hass = _actuator()

    status = asyncio.run(actuator.async_apply("discharge", 1234, min_interval_s=10))

    assert status == "mode_started"
    calls = _calls(hass)
    assert calls[-2] == ("number", "set_value", {"entity_id": "number.dp", "value": 1230})
    assert calls[-1] == ("switch", "turn_on", {"entity_id": "switch.d"})


def test_unchanged_power_is_rewritten_after_the_keepalive_interval(monkeypatch):
    actuator, hass = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(actuator_module.time, "monotonic", lambda: clock[0])
    asyncio.run(actuator.async_apply("discharge", 1230, min_interval_s=10))
    hass.services.async_call.reset_mock()

    clock[0] += 30
    assert asyncio.run(actuator.async_apply("discharge", 1230, min_interval_s=10)) == "holding"
    assert hass.services.async_call.await_count == 0

    clock[0] += 31
    assert asyncio.run(actuator.async_apply("discharge", 1232, min_interval_s=10)) == "keepalive"
    assert _calls(hass) == [
        ("switch", "turn_off", {"entity_id": "switch.d"}),
        ("number", "set_value", {"entity_id": "number.dp", "value": 1230}),
        ("switch", "turn_on", {"entity_id": "switch.d"}),
    ]


def test_a_mode_the_inverter_dropped_is_started_again(monkeypatch):
    actuator, hass = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(actuator_module.time, "monotonic", lambda: clock[0])
    asyncio.run(actuator.async_apply("discharge", 1230, min_interval_s=10))
    hass.services.async_call.reset_mock()
    states = hass.states.get.return_value
    hass.states.get.return_value = SimpleNamespace(state="off", attributes=states.attributes)

    clock[0] += 5
    assert asyncio.run(actuator.async_apply("discharge", 1230, min_interval_s=10)) == "holding"

    clock[0] += 20
    assert asyncio.run(actuator.async_apply("discharge", 1230, min_interval_s=10)) == "restarted"
    assert _calls(hass)[-1] == ("switch", "turn_on", {"entity_id": "switch.d"})


def test_power_rounding_never_exceeds_requested_limit():
    actuator, hass = _actuator()
    hass.states.get.return_value.attributes["step"] = 100

    asyncio.run(actuator.async_apply("discharge", 151, min_interval_s=10))

    assert _calls(hass)[-2] == ("number", "set_value", {"entity_id": "number.dp", "value": 100})


def test_unrepresentable_power_does_not_enable_a_mode():
    actuator, hass = _actuator()
    hass.states.get.return_value.attributes["min"] = 1000

    with pytest.raises(ActuatorError):
        asyncio.run(actuator.async_apply("discharge", 500, min_interval_s=10))

    assert all(call[1] != "turn_on" for call in _calls(hass))


def test_reductions_are_not_delayed_by_rate_limiting(monkeypatch):
    actuator, hass = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(actuator_module.time, "monotonic", lambda: clock[0])
    asyncio.run(actuator.async_apply("discharge", 5000, min_interval_s=30))
    hass.services.async_call.reset_mock()
    clock[0] += 1

    assert (
        asyncio.run(actuator.async_apply("discharge", 1000, min_interval_s=30)) == "power_updated"
    )
    assert _calls(hass) == [("number", "set_value", {"entity_id": "number.dp", "value": 1000})]
