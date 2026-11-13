"""Tests for the idle period between opposite directions."""

import asyncio

from custom_components.nexus_controller import actuator as actuator_module
from tests.test_actuator import _actuator


def _apply(actuator, mode, delay):
    return asyncio.run(actuator.async_apply(mode, 1000, min_interval_s=10, direction_delay_s=delay))


def test_reversing_goes_through_an_idle_period(monkeypatch):
    actuator, _ = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(actuator_module.time, "monotonic", lambda: clock[0])

    assert _apply(actuator, "discharge", 60) == "mode_started"
    clock[0] += 5
    assert _apply(actuator, "charge", 60) == "idle_before_reverse"
    clock[0] += 30
    assert _apply(actuator, "charge", 60) == "waiting_direction_delay"
    clock[0] += 45
    assert _apply(actuator, "charge", 60) == "mode_started"


def test_same_direction_restarts_without_waiting(monkeypatch):
    actuator, _ = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(actuator_module.time, "monotonic", lambda: clock[0])

    _apply(actuator, "discharge", 60)
    asyncio.run(actuator.async_release())
    clock[0] += 1

    assert _apply(actuator, "discharge", 60) == "mode_started"


def test_even_zero_configured_delay_waits_past_keepalive(monkeypatch):
    actuator, _ = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(actuator_module.time, "monotonic", lambda: clock[0])

    _apply(actuator, "discharge", 0)

    assert _apply(actuator, "charge", 0) == "idle_before_reverse"
    clock[0] += 74
    assert _apply(actuator, "charge", 0) == "waiting_direction_delay"
    clock[0] += 1
    assert _apply(actuator, "charge", 0) == "mode_started"
