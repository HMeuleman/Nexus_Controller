"""Offline checks for diagnostic-only consumption forecasts."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import yaml

from custom_components.nexus_controller.config_flow import _config_schema
from custom_components.nexus_controller.const import (
    CONF_DEMAND_WEATHER,
    CONF_HEATPUMP_POWER,
    CONF_HOUSE_ENERGY,
)
from custom_components.nexus_controller.coordinator import NexusCoordinator
from custom_components.nexus_controller.demand import (
    DemandHistory,
    heatpump_power,
    parse_temperatures,
)


def test_quarter_counter_snapshots_remove_heatpump_and_ignore_negative_spike():
    history = DemandHistory()
    start = datetime(2026, 11, 10, 12, tzinfo=UTC)
    for seconds in range(0, 901, 10):
        energy = 10 + seconds / 3600 * 2
        history.observe(
            start + timedelta(seconds=seconds), -80000 if seconds == 30 else energy, 1000, UTC
        )
    assert history.days["2026-11-10"]["48"] == pytest.approx(0.25)


def test_counter_reset_inside_quarter_is_not_consumption():
    history = DemandHistory()
    start = datetime(2026, 11, 10, 12, tzinfo=UTC)
    for seconds in range(0, 901, 10):
        history.observe(start + timedelta(seconds=seconds), 10 if seconds < 450 else 0, 0, UTC)
    assert history.days == {}
    assert history.rejected_quarters == 1


def test_forecast_has_graph_ready_slots_without_inventing_missing_baseline():
    history = DemandHistory()
    now = datetime(2026, 11, 10, 6, tzinfo=UTC)
    temperatures = [(now + timedelta(hours=index), 5.0) for index in range(25)]
    result = history.forecast(now, temperatures, UTC)
    assert len(result["slots"]) == 96
    assert result["household_kwh"] is None
    assert result["slots"][0]["baseline_w"] is None
    assert result["slots"][0]["heatpump_w"] == pytest.approx(1106.7, abs=0.1)


def test_forecast_uses_previous_days_and_excludes_today():
    history = DemandHistory()
    history.restore(
        {
            "days": {
                "2026-11-07": {str(index): 0.25 for index in range(96)},
                "2026-11-08": {str(index): 0.25 for index in range(96)},
                "2026-11-09": {str(index): 0.25 for index in range(96)},
                "2026-11-10": {"24": 99},
            }
        }
    )
    now = datetime(2026, 11, 10, 6, tzinfo=UTC)
    result = history.forecast(
        now, [(now + timedelta(hours=index), 5.0) for index in range(25)], UTC
    )
    assert result["status"] == "ready"
    assert result["slots"][0]["baseline_w"] == 1000
    assert result["slots"][0]["history_days"] == 3


def test_local_schedule_and_missing_weather():
    zone = ZoneInfo("Europe/Amsterdam")
    assert heatpump_power(5, datetime(2026, 11, 10, 5, tzinfo=UTC).astimezone(zone)) > 0
    assert heatpump_power(5, datetime(2026, 11, 10, 22, tzinfo=UTC).astimezone(zone)) == 0
    result = DemandHistory().forecast(datetime(2026, 11, 10, 6, tzinfo=UTC), [], zone)
    assert result["heatpump_kwh"] is None
    assert result["slots"][0]["heatpump_w"] is None


def test_weather_parser_handles_units_and_rejects_bad_rows():
    forecast = [
        {"datetime": "2026-11-10T06:00:00Z", "temperature": 41},
        {"datetime": "2026-11-10T07:00:00", "temperature": 10},
        {"datetime": "bad", "temperature": 10},
    ]
    assert parse_temperatures(forecast, "°F") == [(datetime(2026, 11, 10, 6, tzinfo=UTC), 5)]
    assert parse_temperatures(forecast, "K") == []


def test_missing_samples_and_restarts_do_not_become_zero_load():
    history = DemandHistory()
    start = datetime(2026, 11, 10, 12, tzinfo=UTC)
    for seconds in (0, 10, 20, 850, 860, 870, 900):
        history.observe(start + timedelta(seconds=seconds), 10 + seconds / 3600, 0, UTC)
    assert history.days == {}
    restored = DemandHistory()
    restored.restore(history.as_dict())
    assert restored.days == {}
    assert restored._last is None


def test_midnight_counter_reset_does_not_create_spike():
    history = DemandHistory()
    start = datetime(2026, 11, 10, 23, 45, tzinfo=UTC)
    for seconds in range(0, 1801, 10):
        counter = 40 + seconds / 3600 if seconds < 900 else (seconds - 900) / 3600
        history.observe(start + timedelta(seconds=seconds), counter, 0, UTC)
    assert history.days["2026-11-10"]["95"] == pytest.approx(0.25)
    assert history.days["2026-11-11"]["0"] == pytest.approx(0.25)


def test_dst_fold_quarter_is_not_learned_and_forecast_is_96_real_quarters():
    zone = ZoneInfo("Europe/Amsterdam")
    history = DemandHistory()
    start = datetime(2026, 10, 25, 0, tzinfo=UTC)
    for seconds in range(0, 901, 10):
        history.observe(start + timedelta(seconds=seconds), seconds / 3600, 0, zone)
    assert history.days == {}
    slots = history.forecast(start, [], zone)["slots"]
    times = [datetime.fromisoformat(slot["start"]) for slot in slots]
    assert len(times) == len(set(times)) == 96
    assert all(
        (after - before).total_seconds() == 900
        for before, after in zip(times, times[1:], strict=False)
    )


def test_forecast_does_not_interpolate_across_missing_weather_hour():
    now = datetime(2026, 11, 10, 6, tzinfo=UTC)
    slots = DemandHistory().forecast(now, [(now, 5), (now + timedelta(hours=2), 8)], UTC)["slots"]
    assert slots[0]["heatpump_w"] is None


def test_optional_inputs_and_apexcharts_attribute_contract():
    schema = _config_schema()
    keys = {key.schema for key in schema.schema}
    assert {CONF_HOUSE_ENERGY, CONF_HEATPUMP_POWER, CONF_DEMAND_WEATHER} <= keys
    card = yaml.safe_load((Path(__file__).parents[1] / "dashboard_demand.yaml").read_text())
    assert card["type"] == "custom:apexcharts-card"
    for series, field in zip(
        card["series"], ("total_w", "baseline_w", "heatpump_w", "temperature_c"), strict=True
    ):
        assert "entity.attributes.slots" in series["data_generator"]
        assert f"slot.{field}" in series["data_generator"]


def test_background_weather_request_only_reads_selected_weather_entity():
    response = {
        "weather.selected": {"forecast": [{"datetime": "2026-11-10T06:00:00Z", "temperature": 5}]}
    }
    call = AsyncMock(return_value=response)
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(
            services=SimpleNamespace(async_call=call),
            states=SimpleNamespace(
                get=lambda entity: SimpleNamespace(attributes={"temperature_unit": "°C"})
            ),
        )
    )

    asyncio.run(NexusCoordinator._async_demand_weather(coordinator, "weather.selected"))

    call.assert_awaited_once_with(
        "weather",
        "get_forecasts",
        {"entity_id": "weather.selected", "type": "hourly"},
        blocking=True,
        return_response=True,
    )
    assert coordinator._demand_temperatures == [(datetime(2026, 11, 10, 6, tzinfo=UTC), 5)]
    assert coordinator._demand_weather_status == "ok"


def test_weather_failure_is_diagnostic_and_does_not_change_controller_settings():
    settings = {"control_enabled": True}
    coordinator = SimpleNamespace(
        settings=settings.copy(),
        hass=SimpleNamespace(
            services=SimpleNamespace(async_call=AsyncMock(side_effect=RuntimeError("offline")))
        ),
    )
    asyncio.run(NexusCoordinator._async_demand_weather(coordinator, "weather.selected"))
    assert coordinator._demand_weather_status == "request_failed"
    assert coordinator.settings == settings


def test_weather_request_bridges_to_first_hour_using_current_temperature(monkeypatch):
    from custom_components.nexus_controller import coordinator as coordinator_module

    now = datetime(2026, 11, 10, 6, 10, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: now)
    state = SimpleNamespace(attributes={"temperature_unit": "°C", "temperature": 4.0})
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(
            services=SimpleNamespace(
                async_call=AsyncMock(
                    return_value={
                        "weather.selected": {
                            "forecast": [{"datetime": "2026-11-10T07:00:00Z", "temperature": 5.0}]
                        }
                    }
                )
            ),
            states=SimpleNamespace(get=lambda entity: state),
        )
    )
    asyncio.run(NexusCoordinator._async_demand_weather(coordinator, "weather.selected"))
    assert coordinator._demand_temperatures[0] == (now, 4.0)
    assert coordinator._demand_temperatures[1] == (datetime(2026, 11, 10, 7, tzinfo=UTC), 5.0)


def test_coordinator_collects_converted_units_without_waiting_for_weather():
    async def scenario():
        now = datetime(2026, 11, 10, 6, tzinfo=UTC)
        states = {
            "sensor.energy": SimpleNamespace(
                state="1000", attributes={"unit_of_measurement": "Wh"}
            ),
            "sensor.heat": SimpleNamespace(state="1.5", attributes={"unit_of_measurement": "kW"}),
        }
        history = DemandHistory()
        original_observe = history.observe
        history.observe = MagicMock(wraps=original_observe)
        weather = AsyncMock()
        coordinator = SimpleNamespace(
            _demand_sources=lambda: ["sensor.energy", "sensor.heat"],
            configured_entity_id=lambda key: "weather.selected",
            hass=SimpleNamespace(
                states=SimpleNamespace(get=states.get),
                config=SimpleNamespace(time_zone="Europe/Amsterdam"),
                async_create_task=asyncio.create_task,
            ),
            _demand_history=history,
            _demand_store=SimpleNamespace(async_delay_save=MagicMock()),
            _demand_data=lambda: {},
            _demand_task=None,
            _demand_weather_attempt=None,
            _demand_weather_updated=None,
            _demand_temperatures=[],
            _demand_weather_status="not_requested",
            _async_demand_weather=weather,
        )
        data = {}
        NexusCoordinator._update_demand_forecast(coordinator, data, now)
        history.observe.assert_called_once_with(now, 1.0, 1500.0, ZoneInfo("Europe/Amsterdam"))
        weather.assert_not_awaited()
        await coordinator._demand_task
        NexusCoordinator._update_demand_forecast(coordinator, data, now + timedelta(seconds=10))
        weather.assert_awaited_once()
        assert data["demand_forecast"] is None
        assert coordinator.demand_details["diagnostic_only"] is True

    asyncio.run(scenario())
