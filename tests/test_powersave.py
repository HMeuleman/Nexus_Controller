"""Offline safety scenarios for inverter powersave."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.nexus_controller import coordinator as coordinator_module
from custom_components.nexus_controller.config_flow import _config_schema, _validation_error
from custom_components.nexus_controller.const import (
    CONF_BATTERY_SOC,
    CONF_CHARGE_ENABLE,
    CONF_DISCHARGE_ENABLE,
    CONF_INVERTER_POWER,
    SET_CONTROL_ENABLED,
    SET_PLANNER_OVERRIDE,
    SET_POWERSAVE_ENABLED,
    SET_POWERSAVE_INTERVAL,
    SET_POWERSAVE_LEAD,
    SET_RESERVED_ROOM,
    SETTING_DEFAULTS,
)
from custom_components.nexus_controller.coordinator import NexusCoordinator
from custom_components.nexus_controller.dispatch import DispatchPlan
from custom_components.nexus_controller.powersave import (
    PowerSaveMemory,
    PowerSaveSettings,
    decide_powersave,
    schedule_window,
)


def decision(**overrides):
    inputs = {
        "now_s": 1800.0,
        "inverter_on": True,
        "enabled": True,
        "planner_enabled": True,
        "plan_valid": True,
        "schedule_end_s": 100000.0,
        "next_work_s": None,
        "soc_pct": 50.0,
        "battery_power_w": 0.0,
        "readings_fresh": True,
        "manual_active": False,
        "memory": PowerSaveMemory(0.0, 0.0, 0.0, 0.0),
        "settings": PowerSaveSettings(),
    }
    return decide_powersave(**(inputs | overrides))


def test_off_only_after_startup_and_activity_cooldown():
    assert decision(now_s=899).reason == "cooldown"
    assert decision(now_s=900).power == "off"
    active = decision(now_s=1800, battery_power_w=-1)
    assert active.reason == "battery_active"
    assert decision(now_s=2699, memory=active.memory).reason == "cooldown"
    assert decision(now_s=2700, memory=active.memory).power == "off"


@pytest.mark.parametrize("power", [-2000, -0.01, 0.01, 2000])
def test_any_nonzero_battery_power_prevents_shutdown(power):
    assert decision(battery_power_w=power).reason == "battery_active"


def test_wake_before_work_and_do_not_create_short_off_periods():
    assert decision(next_work_s=1800 + 300).reason == "planned_work"
    assert decision(next_work_s=1800 + 1199).reason == "idle_period_too_short"
    assert decision(next_work_s=1800 + 1200).power == "off"
    assert decision(inverter_on=False, next_work_s=2101).power == "off"
    assert decision(inverter_on=False, next_work_s=2100).power == "on"


def test_wake_for_daily_maintenance_and_track_continuous_on_time():
    memory = PowerSaveMemory(0.0, 0.0, 0.0, None)
    assert decision(now_s=84599, inverter_on=False, memory=memory).power == "off"
    due = decision(now_s=84600, inverter_on=False, memory=memory)
    assert due.reason == "maintenance_due"
    switched_on = decision(now_s=84601, inverter_on=True, memory=due.memory)
    assert not switched_on.ready_for_control
    held = decision(now_s=86400, inverter_on=True, memory=switched_on.memory)
    assert held.reason == "maintenance_due"
    completed = decision(now_s=86401, inverter_on=True, memory=held.memory)
    assert completed.memory.maintenance_s == 86401
    assert completed.power == "off"


def test_warmup_after_confirmed_power_on():
    off = decision(inverter_on=False, manual_active=True)
    on = decision(now_s=1801, inverter_on=True, memory=off.memory, manual_active=True)
    assert not on.ready_for_control
    assert not decision(
        now_s=1860, inverter_on=True, memory=on.memory, manual_active=True
    ).ready_for_control
    assert decision(
        now_s=1861, inverter_on=True, memory=on.memory, manual_active=True
    ).ready_for_control


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"soc_pct": 8.0}, "low_soc"),
        ({"soc_pct": None}, "measurements_unavailable"),
        ({"battery_power_w": None}, "measurements_unavailable"),
        ({"readings_fresh": False}, "measurements_unavailable"),
        ({"plan_valid": False}, "plan_unavailable"),
        ({"schedule_end_s": None}, "plan_unavailable"),
        ({"next_work_s": 100001}, "plan_unavailable"),
        ({"planner_enabled": False}, "powersave_disabled"),
        ({"enabled": False}, "powersave_disabled"),
        ({"manual_active": True}, "manual_override"),
        ({"inverter_on": None}, "inverter_state_unavailable"),
        (
            {"settings": replace(PowerSaveSettings(), maintenance_duration_s=86400)},
            "invalid_settings_or_clock",
        ),
    ],
)
def test_invalid_or_unsafe_inputs_require_power_on(overrides, reason):
    result = decision(**overrides)
    assert result.power == "on"
    assert result.reason == reason


def test_forecast_must_cover_the_minimum_off_period_and_wake_lead():
    assert decision(schedule_end_s=2999).reason == "idle_period_too_short"
    assert decision(schedule_end_s=3000).power == "off"
    assert decision(inverter_on=False, schedule_end_s=2101).power == "off"
    assert decision(inverter_on=False, schedule_end_s=2100).reason == "plan_ending"


def test_programmable_values_are_used():
    settings = replace(PowerSaveSettings(), cooldown_s=300, lead_s=120, min_soc_pct=12)
    assert decision(now_s=300, settings=settings).power == "off"
    assert decision(settings=settings, soc_pct=12).reason == "low_soc"
    assert decision(settings=settings, next_work_s=1920).reason == "planned_work"


def test_schedule_window_requires_contiguous_current_and_future_slots():
    start = datetime(2026, 11, 12, 12, tzinfo=UTC)
    now = start + timedelta(minutes=5)
    slots = [
        {"start": start.isoformat(), "duration_hours": 0.25, "power_w": 0},
        {
            "start": (start + timedelta(minutes=15)).isoformat(),
            "duration_hours": 0.25,
            "power_w": -1000,
        },
    ]
    assert schedule_window(slots, now) == (
        (start + timedelta(minutes=30)).timestamp(),
        (start + timedelta(minutes=15)).timestamp(),
    )
    assert schedule_window(slots[1:], now) is None
    slots[1]["start"] = (start + timedelta(minutes=30)).isoformat()
    assert schedule_window(slots, now) is None
    slots[1]["start"] = (start + timedelta(minutes=15)).isoformat()
    slots[1]["power_w"] = float("nan")
    assert schedule_window(slots, now) is None


def integration_coordinator(monkeypatch, inverter="on"):
    clock = [2000.0]
    monkeypatch.setattr(coordinator_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    states = {
        "switch.inverter": SimpleNamespace(state=inverter),
        "switch.charge": SimpleNamespace(state="off"),
        "switch.discharge": SimpleNamespace(state="off"),
    }
    entity_ids = {
        CONF_INVERTER_POWER: "switch.inverter",
        CONF_CHARGE_ENABLE: "switch.charge",
        CONF_DISCHARGE_ENABLE: "switch.discharge",
    }
    slots = [
        {
            "start": (now + timedelta(minutes=15 * index)).isoformat(),
            "duration_hours": 0.25,
            "power_w": 0,
        }
        for index in range(8)
    ]
    coordinator = SimpleNamespace(
        configured_entity_id=entity_ids.get,
        hass=SimpleNamespace(
            states=SimpleNamespace(get=states.get), services=SimpleNamespace(async_call=AsyncMock())
        ),
        settings={**SETTING_DEFAULTS, SET_CONTROL_ENABLED: True, SET_POWERSAVE_ENABLED: True},
        sample_estimator=SimpleNamespace(interval_s=10.0),
        _planner_measurement_ages=lambda moment: (1, 1, 1, 1, 1),
        _planner_action=lambda *args: DispatchPlan("idle", 0, "planned_idle_or_limit"),
        experimental_quarter_plan={"slots": slots},
        _powersave_memory=PowerSaveMemory(0, 0, 0, 0 if inverter == "on" else None),
        _powersave_saved_maintenance=0,
        _powersave_store=SimpleNamespace(async_delay_save=MagicMock()),
        _powersave_data=lambda: {},
        _powersave_wake_pending=False,
        _manual_mode=None,
    )
    coordinator._async_set_inverter_power = lambda on: NexusCoordinator._async_set_inverter_power(
        coordinator, on
    )

    async def step(power=0.0, soc=50.0):
        return await NexusCoordinator._async_powersave_step(
            coordinator, {CONF_BATTERY_SOC: soc}, now, (0.0, 0.0, 0.0), power, (10000, 10000)
        )

    return coordinator, states, clock, step


def test_integration_only_switches_off_when_forced_modes_are_confirmed_idle(monkeypatch):
    coordinator, states, _, step = integration_coordinator(monkeypatch)
    states["switch.discharge"].state = "on"
    assert asyncio.run(step()) is True
    coordinator.hass.services.async_call.assert_not_awaited()

    states["switch.discharge"].state = "off"
    assert asyncio.run(step()) is False
    coordinator.hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.inverter"}, blocking=True
    )
    assert coordinator._powersave_memory.on_since_s is None


def test_integration_wakes_on_low_soc_and_waits_for_confirmed_power_and_warmup(monkeypatch):
    coordinator, states, clock, step = integration_coordinator(monkeypatch, inverter="off")
    assert asyncio.run(step(soc=7)) is False
    coordinator.hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_on", {"entity_id": "switch.inverter"}, blocking=True
    )
    states["switch.inverter"].state = "on"
    clock[0] += 1
    assert asyncio.run(step(soc=7)) is False
    clock[0] += 59
    assert asyncio.run(step(soc=7)) is False
    clock[0] += 1
    assert asyncio.run(step(soc=7)) is True


@pytest.mark.parametrize("condition", ["planner", "control", "measurement", "plan", "manual"])
def test_integration_fails_on_when_not_safe_to_power_off(monkeypatch, condition):
    coordinator, _, _, step = integration_coordinator(monkeypatch, inverter="off")
    if condition == "planner":
        coordinator.settings[SET_PLANNER_OVERRIDE] = False
    elif condition == "control":
        coordinator.settings[SET_CONTROL_ENABLED] = False
    elif condition == "measurement":
        coordinator._planner_measurement_ages = lambda moment: None
    elif condition == "plan":
        coordinator._planner_action = lambda *args: DispatchPlan(
            "idle", 0, "planner_unavailable_or_stale"
        )
    else:
        coordinator._manual_mode = "charge"
    assert asyncio.run(step()) is False
    coordinator.hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_on", {"entity_id": "switch.inverter"}, blocking=True
    )


def test_integration_does_not_assume_power_on_when_service_fails(monkeypatch):
    coordinator, _, _, step = integration_coordinator(monkeypatch, inverter="off")
    coordinator.hass.services.async_call.side_effect = RuntimeError("write failed")
    assert asyncio.run(step(soc=7)) is False
    assert coordinator._powersave_memory.on_since_s is None


def test_invalid_runtime_settings_request_wake_and_block_control(monkeypatch):
    coordinator, _, _, step = integration_coordinator(monkeypatch, inverter="off")
    coordinator.settings[SET_POWERSAVE_INTERVAL] = "invalid"
    assert asyncio.run(step()) is False
    coordinator.hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_on", {"entity_id": "switch.inverter"}, blocking=True
    )


def test_wake_lead_never_undercuts_required_warmup(monkeypatch):
    coordinator, _, _, step = integration_coordinator(monkeypatch, inverter="off")
    coordinator.settings[SET_POWERSAVE_LEAD] = 0
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    slots = coordinator.experimental_quarter_plan["slots"]
    slots[0]["start"] = (now - timedelta(minutes=14)).isoformat()
    for index, slot in enumerate(slots[1:]):
        slot["start"] = (now + timedelta(minutes=1 + 15 * index)).isoformat()
    slots[1]["power_w"] = 1000

    assert asyncio.run(step()) is False
    coordinator.hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_on", {"entity_id": "switch.inverter"}, blocking=True
    )


def test_integration_never_switches_off_on_battery_activity_or_unknown_force_mode(monkeypatch):
    coordinator, states, _, step = integration_coordinator(monkeypatch)
    assert asyncio.run(step(power=-0.01)) is True
    coordinator.hass.services.async_call.assert_not_awaited()
    coordinator._powersave_memory = PowerSaveMemory(0, 0, 0, 0)
    states["switch.discharge"].state = "unknown"
    assert asyncio.run(step()) is True
    coordinator.hass.services.async_call.assert_not_awaited()


def test_integration_does_not_write_when_powersave_was_never_enabled(monkeypatch):
    coordinator, _, _, step = integration_coordinator(monkeypatch, inverter="off")
    coordinator.settings[SET_POWERSAVE_ENABLED] = False
    assert asyncio.run(step(soc=7)) is False
    coordinator.hass.services.async_call.assert_not_awaited()


def test_inverter_power_events_restart_the_warmup_timer(monkeypatch):
    coordinator, _, clock, _ = integration_coordinator(monkeypatch)
    coordinator.settings[SET_POWERSAVE_ENABLED] = False
    coordinator.settings[SET_CONTROL_ENABLED] = False
    off = SimpleNamespace(data={"new_state": SimpleNamespace(state="off")})
    on = SimpleNamespace(data={"new_state": SimpleNamespace(state="on")})
    NexusCoordinator._on_inverter_power_event(coordinator, off)
    assert coordinator._powersave_memory.on_since_s is None
    clock[0] += 1
    NexusCoordinator._on_inverter_power_event(coordinator, on)
    assert coordinator._powersave_memory.on_since_s == clock[0]
    clock[0] += 10
    unchanged = SimpleNamespace(
        data={"old_state": SimpleNamespace(state="on"), "new_state": SimpleNamespace(state="on")}
    )
    NexusCoordinator._on_inverter_power_event(coordinator, unchanged)
    assert coordinator._powersave_memory.on_since_s == clock[0] - 10


def test_battery_power_event_records_brief_activity(monkeypatch):
    coordinator, _, clock, _ = integration_coordinator(monkeypatch)
    coordinator.settings[SET_POWERSAVE_ENABLED] = False
    event = SimpleNamespace(
        data={"new_state": SimpleNamespace(state="-0.01", attributes={"unit_of_measurement": "kW"})}
    )
    NexusCoordinator._on_powersave_battery_event(coordinator, event)
    assert coordinator._powersave_memory.last_activity_s == clock[0]


def test_powersave_is_off_by_default_and_requires_selected_switch():
    assert SETTING_DEFAULTS[SET_POWERSAVE_ENABLED] is False
    coordinator = SimpleNamespace(
        settings=dict(SETTING_DEFAULTS),
        configured_entity_id=lambda key: None,
        async_refresh=AsyncMock(),
    )
    with pytest.raises(ServiceValidationError, match="Select an inverter power switch"):
        asyncio.run(NexusCoordinator.async_set_powersave(coordinator, True))
    assert coordinator.settings[SET_POWERSAVE_ENABLED] is False


def test_inverter_mapping_is_optional_and_cannot_reuse_a_force_switch():
    keys = [getattr(key, "schema", key) for key in _config_schema().schema]
    assert CONF_INVERTER_POWER in keys
    assert _validation_error({CONF_INVERTER_POWER: "switch.power"}) is None
    assert (
        _validation_error(
            {CONF_INVERTER_POWER: "switch.power", CONF_CHARGE_ENABLE: "switch.power"}
        )
        == "duplicate_entities"
    )


def test_enabling_powersave_requests_boot_wake_without_enabling_control(monkeypatch):
    coordinator, _, _, step = integration_coordinator(monkeypatch, inverter="off")
    coordinator.settings[SET_CONTROL_ENABLED] = False
    coordinator.settings[SET_POWERSAVE_ENABLED] = False
    coordinator.async_refresh = AsyncMock(side_effect=step)

    asyncio.run(NexusCoordinator.async_set_powersave(coordinator, True))

    assert coordinator.settings[SET_POWERSAVE_ENABLED] is True
    assert coordinator.settings[SET_CONTROL_ENABLED] is False
    coordinator.hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_on", {"entity_id": "switch.inverter"}, blocking=True
    )


def test_disabling_powersave_requests_wake_without_assuming_confirmation(monkeypatch):
    coordinator, states, _, step = integration_coordinator(monkeypatch, inverter="off")
    coordinator.async_refresh = AsyncMock(side_effect=step)

    asyncio.run(NexusCoordinator.async_set_powersave(coordinator, False))

    assert coordinator._powersave_wake_pending is True
    coordinator.hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_on", {"entity_id": "switch.inverter"}, blocking=True
    )
    states["switch.inverter"].state = "on"
    assert asyncio.run(step()) is False
    assert coordinator._powersave_wake_pending is False


def test_restart_restores_last_maintenance_deadline(monkeypatch):
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: now)
    store = SimpleNamespace(
        async_load=AsyncMock(return_value={"maintenance_timestamp": now.timestamp() - 23 * 3600}),
        async_delay_save=MagicMock(),
    )
    coordinator = SimpleNamespace(
        _store=SimpleNamespace(async_load=AsyncMock(return_value=None)),
        _powersave_store=store,
        _powersave_data=lambda: {},
        configured_entity_id=lambda key: None,
    )

    asyncio.run(NexusCoordinator.async_setup(coordinator))

    assert coordinator._powersave_memory.started_s == 1000
    assert coordinator._powersave_memory.maintenance_s == pytest.approx(1000 - 23 * 3600)
    result = decision(
        now_s=1000,
        inverter_on=False,
        memory=coordinator._powersave_memory,
        schedule_end_s=100000,
    )
    assert result.power == "on"
    assert result.reason == "cooldown"
    due = decision(
        now_s=2800,
        inverter_on=False,
        memory=coordinator._powersave_memory,
        schedule_end_s=100000,
    )
    assert due.reason == "maintenance_due"


def test_main_control_loop_skips_writes_while_inverter_is_not_ready():
    coordinator = SimpleNamespace(
        configured_entity_id=lambda key: None,
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda entity: None)),
        _battery_power=lambda: 0.0,
        _number=lambda key, default: default,
        settings={SET_RESERVED_ROOM: 1000},
        _read_prices=lambda data, now: None,
        _track=lambda *args: None,
        _read_solar=lambda data, now: 0.0,
        sample_estimator=SimpleNamespace(interval_s=10),
        _discharge_threshold=lambda: 0.0,
        _plan=lambda *args: DispatchPlan("charge", 1000, "planned_grid_charge"),
        _async_powersave_step=AsyncMock(return_value=False),
        _async_control=AsyncMock(),
        _schedule_experimental_quarter_plan=MagicMock(),
        _store=SimpleNamespace(async_delay_save=MagicMock()),
        tracker=SimpleNamespace(as_dict=lambda: {}),
    )

    data = asyncio.run(NexusCoordinator._async_compute(coordinator))

    coordinator._async_control.assert_not_awaited()
    assert data["control_status"] == "inverter_not_ready"