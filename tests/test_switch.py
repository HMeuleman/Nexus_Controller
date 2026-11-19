"""Tests for the battery control switch restore behavior after a restart."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.nexus_controller.const import SET_CONTROL_ENABLED
from custom_components.nexus_controller.switch import NexusControlSwitch


def _switch(state: str | None, fail: bool = False):
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id="test"),
        settings={SET_CONTROL_ENABLED: False},
        async_add_listener=MagicMock(return_value=lambda: None),
    )

    async def set_control(enabled: bool) -> None:
        if fail:
            raise RuntimeError("boom")
        coordinator.settings[SET_CONTROL_ENABLED] = enabled

    coordinator.async_set_control = AsyncMock(side_effect=set_control)
    switch = NexusControlSwitch(coordinator)
    switch.async_get_last_state = AsyncMock(
        return_value=None if state is None else SimpleNamespace(state=state)
    )
    switch.async_write_ha_state = MagicMock()
    return switch, coordinator


def test_control_switch_restores_on_after_restart():
    switch, coordinator = _switch("on")

    asyncio.run(switch.async_added_to_hass())

    coordinator.async_set_control.assert_awaited_once_with(True)
    assert switch.is_on is True
    switch.async_write_ha_state.assert_called_once()


def test_control_switch_restores_off_after_restart():
    switch, coordinator = _switch("off")

    asyncio.run(switch.async_added_to_hass())

    coordinator.async_set_control.assert_awaited_once_with(False)
    assert switch.is_on is False


def test_control_switch_without_stored_state_stays_default():
    switch, coordinator = _switch(None)

    asyncio.run(switch.async_added_to_hass())

    coordinator.async_set_control.assert_not_awaited()
    assert switch.is_on is False


def test_control_switch_survives_restore_failure():
    switch, coordinator = _switch("on", fail=True)

    asyncio.run(switch.async_added_to_hass())

    coordinator.async_set_control.assert_awaited_once_with(True)
    switch.async_write_ha_state.assert_called_once()
