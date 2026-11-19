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
    CONF_HOUSE_POWER,
    CONF_OUTDOOR_TEMP,
)
from custom_components.nexus_controller.coordinator import NexusCoordinator
from custom_components.nexus_controller.demand import (
    DemandHistory,
    normalize_temperature,
    parse_temperatures,
    temperature_at,
)


def test_temperature_is_interpolated_within_an_hour_and_none_across_gaps():
    when = datetime(2026, 11, 10, 12, tzinfo=UTC)
    points = [
        (when - timedelta(minutes=30), 12.0),
        (when + timedelta(minutes=30), 6.0),
        (when + timedelta(hours=4), 4.0),
    ]
    assert temperature_at(points, when) == pytest.approx(9.0)
    assert temperature_at(points, when - timedelta(hours=2)) is None
    assert temperature_at(points, when + timedelta(hours=5)) is None
    assert temperature_at(points, when + timedelta(hours=2)) is None  # 3 h gap
    assert temperature_at([], when) is None


def test_current_temperature_normalizes_units_and_rejects_bad_values():
    assert normalize_temperature(21, "°C") == 21.0
    assert normalize_temperature(41, "°F") == pytest.approx(5.0)
    assert normalize_temperature("bogus", "°C") is None
    assert normalize_temperature(21, "K") is None


def test_quarter_power_samples_remove_heatpump_and_ignore_negative_spike():
    history = DemandHistory()
    history.wp_configured = True
    start = datetime(2026, 11, 10, 12, tzinfo=UTC)
    for seconds in range(0, 901, 10):
        house_w = -80000 if seconds == 30 else 2000
        history.observe(start + timedelta(seconds=seconds), house_w, 1000, None, UTC)
    assert history.days["2026-11-10"]["48"] == pytest.approx(1000)
    assert history.wp_days["2026-11-10"]["48"] == pytest.approx(1000)


def test_quarter_without_valid_heatpump_samples_is_rejected():
    history = DemandHistory()
    history.wp_configured = True
    start = datetime(2026, 11, 10, 12, tzinfo=UTC)
    for seconds in range(0, 901, 10):
        history.observe(start + timedelta(seconds=seconds), 2000, None, None, UTC)
    assert history.days == {}
    assert history.wp_days == {}
    assert history.rejected_quarters == 1


def test_quarters_are_learnable_from_five_minute_statistic_buckets():
    """Recorder buckets start every 5 min; the held last value covers the tail."""
    history = DemandHistory()
    history.wp_configured = True
    start = datetime(2026, 11, 10, 12, tzinfo=UTC)
    for index in range(0, 13):
        history.backfill(
            [(start + timedelta(minutes=5 * index), 2000.0)],
            [(start + timedelta(minutes=5 * index), 500.0)],
            [(start + timedelta(minutes=5 * index), 7.5)],
            UTC,
        )
    assert history.days["2026-11-10"]["48"] == pytest.approx(1500.0)
    assert history.wp_days["2026-11-10"]["48"] == pytest.approx(500.0)
    assert history.temp_days["2026-11-10"]["48"] == pytest.approx(7.5)


def test_forecast_requires_learned_data_and_reports_null_slots():
    history = DemandHistory()
    now = datetime(2026, 11, 10, 6, tzinfo=UTC)
    temperatures = [(now + timedelta(hours=index), 5.0) for index in range(25)]
    result = history.forecast(now, temperatures, UTC)
    assert len(result["slots"]) == 96
    assert result["household_kwh"] is None
    assert result["heatpump_kwh"] is None
    assert result["status"] == "partial"
    assert result["heatpump_configured"] is True
    assert result["slots"][0]["baseline_w"] is None
    assert result["slots"][0]["heatpump_w"] is None
    assert result["slots"][0]["total_w"] is None


def test_forecast_uses_previous_days_and_excludes_today():
    history = DemandHistory()
    history.restore(
        {
            "version": 2,
            "days": {
                "2026-11-07": {str(index): 1000.0 for index in range(96)},
                "2026-11-08": {str(index): 1000.0 for index in range(96)},
                "2026-11-09": {str(index): 1000.0 for index in range(96)},
                "2026-11-10": {"24": 99_000},
            },
        }
    )
    history.wp_configured = True
    now = datetime(2026, 11, 10, 6, tzinfo=UTC)
    result = history.forecast(
        now, [(now + timedelta(hours=index), 5.0) for index in range(25)], UTC
    )
    assert result["status"] == "partial"  # heat-pump profile has no days yet
    assert result["slots"][0]["baseline_w"] == 1000
    assert result["slots"][0]["heatpump_w"] is None
    assert result["slots"][0]["total_w"] is None
    assert result["slots"][0]["history_days"] == 3
    assert result["household_kwh"] is None


def test_house_only_forecast_uses_baseline_as_total():
    history = DemandHistory()
    history.restore(
        {
            "version": 2,
            "days": {
                f"2026-11-{day:02d}": {str(index): 1000.0 for index in range(96)}
                for day in (7, 8, 9)
            },
        }
    )
    now = datetime(2026, 11, 10, 6, tzinfo=UTC)
    result = history.forecast(now, [], UTC, house_only=True)
    assert result["status"] == "ready"
    assert result["heatpump_configured"] is False
    assert result["heatpump_kwh"] is None
    assert result["household_kwh"] == pytest.approx(24.0)
    slot = result["slots"][0]
    assert slot["baseline_w"] == slot["total_w"]
    assert slot["heatpump_w"] is None


def test_heatpump_mean_is_learned_without_a_fixed_schedule():
    wp = {str(index): 500.0 for index in range(96)}
    history = DemandHistory()
    history.restore(
        {
            "version": 2,
            "days": {
                f"2026-11-{day:02d}": {str(index): 3000.0 for index in range(96)}
                for day in (7, 8, 9)
            },
            "wp_days": {f"2026-11-{day:02d}": wp for day in (7, 8, 9)},
        }
    )
    now = datetime(2026, 11, 10, 6, tzinfo=UTC)
    result = history.forecast(now, [], UTC)
    assert result["status"] == "ready"
    slot = result["slots"][0]
    assert slot["baseline_w"] == 3000.0
    assert slot["heatpump_w"] == 500.0
    assert slot["total_w"] == 3500.0
    assert result["heatpump_kwh"] == pytest.approx(12.0)
    assert result["household_kwh"] == pytest.approx(84.0)


def test_temperature_correction_is_fitted_per_quarter_and_not_linear_total():
    history = DemandHistory()
    days = {}
    wp_days = {}
    temp_days = {}
    for offset, (wp, temp) in enumerate(
        ((1000.0, 10.0), (2000.0, 5.0), (3000.0, 0.0)), start=1
    ):
        day = (datetime(2026, 11, 10) - timedelta(days=offset)).date().isoformat()
        days[day] = {"48": 100.0}
        wp_days[day] = {"48": wp}
        temp_days[day] = {"48": temp}
    history.restore({"version": 2, "days": days, "wp_days": wp_days, "temp_days": temp_days})
    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    hours = [(now + timedelta(hours=index), 0.0) for index in range(25)]
    # Slope of -200 W/deg C: hotter means less heat-pump power.
    predicted = history.forecast(now, hours, UTC)["slots"][0]
    assert predicted["baseline_w"] == 100.0
    assert predicted["heatpump_w"] == pytest.approx(3000.0)
    hours_warm = [(now + timedelta(hours=index), 15.0) for index in range(25)]
    predicted_warm = history.forecast(now, hours_warm, UTC)["slots"][0]
    # 2000 + (-200) * (15 - 5) = 0; the floor keeps it from going negative.
    assert predicted_warm["heatpump_w"] == pytest.approx(0.0)
    # Without any temperature points the plain seven-day mean is used.
    plain = history.forecast(now, [], UTC)["slots"][0]
    assert plain["heatpump_w"] == 2000.0
    assert plain["temperature_c"] is None


def test_temperature_correction_ignores_noise_and_clamps_extremes():
    history = DemandHistory()
    days = {}
    wp_days = {}
    temp_days = {}
    for offset in range(1, 6):
        day = (datetime(2026, 11, 10) - timedelta(days=offset)).date().isoformat()
        days[day] = {"48": 100.0}
        wp_days[day] = {"48": 2000.0}
        temp_days[day] = {"48": 5.0}  # identical temperatures: variance ~0
    history.restore({"version": 2, "days": days, "wp_days": wp_days, "temp_days": temp_days})
    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    for predicted in (0.0, 25.0):
        result = history.forecast(
            now, [(now + timedelta(hours=index), predicted) for index in range(25)], UTC
        )
        assert result["slots"][0]["heatpump_w"] == 2000.0

    # An absurd fit from extreme spread cannot leave a sane band far behind.
    days2, wp2, temp2 = {}, {}, {}
    for offset, (wp, temp) in enumerate(
        ((0.0, -5.0), (3000.0, 10.0), (6000.0, 25.0)), start=1
    ):
        day = (datetime(2026, 11, 10) - timedelta(days=offset)).date().isoformat()
        days2[day] = {"48": 100.0}
        wp2[day] = {"48": wp}
        temp2[day] = {"48": temp}
    clamped = DemandHistory()
    clamped.restore({"version": 2, "days": days2, "wp_days": wp2, "temp_days": temp2})
    result = clamped.forecast(
        now,
        [(now + timedelta(hours=index), 30.0) for index in range(25)],
        UTC,
    )
    assert result["slots"][0]["heatpump_w"] == pytest.approx(7000.0)


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
        history.observe(start + timedelta(seconds=seconds), 500.0, 0, None, UTC)
    assert history.days == {}
    restored = DemandHistory()
    restored.restore(history.as_dict())
    assert restored.days == {}
    assert restored._last_house is None


def test_quarters_are_learned_across_midnight():
    history = DemandHistory()
    start = datetime(2026, 11, 10, 23, 45, tzinfo=UTC)
    for seconds in range(0, 1801, 10):
        history.observe(start + timedelta(seconds=seconds), 500.0, None, None, UTC)
    assert history.days["2026-11-10"]["95"] == pytest.approx(500.0)
    assert history.days["2026-11-11"]["0"] == pytest.approx(500.0)


def test_stored_counters_from_older_versions_are_not_read_as_watts():
    history = DemandHistory()
    history.restore({"version": 1, "days": {"2026-11-09": {"0": 0.25}}, "rejected_quarters": 0})
    assert history.days == {}
    history.restore({"days": {"2026-11-09": {"0": 500.0}}})
    assert history.days == {}
    history.restore({"version": 2, "days": {"2026-11-09": {"96": 500.0}}})
    assert history.days["2026-11-09"] == {}
    history.restore({"version": 2, "days": {"2026-11-09": {"0": -5.0}}})
    assert history.days["2026-11-09"] == {}


def test_dst_fold_quarter_is_not_learned_and_forecast_is_96_real_quarters():
    zone = ZoneInfo("Europe/Amsterdam")
    history = DemandHistory()
    start = datetime(2026, 10, 25, 0, tzinfo=UTC)
    for seconds in range(0, 901, 10):
        history.observe(start + timedelta(seconds=seconds), 1000.0, None, None, zone)
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
    assert slots[0]["temperature_c"] is None


def test_optional_inputs_and_apexcharts_attribute_contract():
    schema = _config_schema()
    keys = {key.schema for key in schema.schema}
    assert {
        CONF_HOUSE_POWER,
        CONF_HEATPUMP_POWER,
        CONF_OUTDOOR_TEMP,
        CONF_DEMAND_WEATHER,
    } <= keys
    card = yaml.safe_load((Path(__file__).parents[1] / "dashboard_demand.yaml").read_text())
    assert card["type"] == "custom:apexcharts-card"
    for series, field in zip(
        card["series"], ("total_w", "baseline_w", "heatpump_w", "temperature_c"), strict=True
    ):
        assert "entity.attributes.slots" in series["data_generator"]
        assert f"slot.{field}" in series["data_generator"]
    history = yaml.safe_load((Path(__file__).parents[1] / "dashboard_history.yaml").read_text())
    assert history["type"] == "custom:apexcharts-card"
    assert history["graph_span"] == "48h"
    assert len(history["series"]) == 4
    for series in history["series"]:
        assert series["entity"].startswith("sensor.replace_with_your_")


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
            "sensor.power": SimpleNamespace(
                state="2.0", attributes={"unit_of_measurement": "kW"}
            ),
            "sensor.heat": SimpleNamespace(state="1.5", attributes={"unit_of_measurement": "kW"}),
            "weather.selected": SimpleNamespace(
                state="rainy", attributes={"temperature": 5, "temperature_unit": "°C"}
            ),
        }
        history = DemandHistory()
        original_observe = history.observe
        history.observe = MagicMock(wraps=original_observe)
        weather = AsyncMock()
        coordinator = SimpleNamespace(
            _demand_sources=lambda: ["sensor.power", "sensor.heat", None],
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
        history.observe.assert_called_once_with(
            now, 2000.0, 1500.0, 5.0, ZoneInfo("Europe/Amsterdam")
        )
        weather.assert_not_awaited()
        await coordinator._demand_task
        NexusCoordinator._update_demand_forecast(coordinator, data, now + timedelta(seconds=10))
        weather.assert_awaited_once()
        assert data["demand_forecast"] is None
        assert data["heatpump_forecast"] is None
        # Learning has not started: the recorded forecast-for-now reads 0, not null.
        assert data["demand_forecast_now"] == 0.0
        assert data["demand_temp_forecast"] is None
        assert data["demand_temp_actual"] == 5.0
        assert coordinator.demand_details["diagnostic_only"] is True
        assert coordinator.demand_details["heatpump_configured"] is True
        assert coordinator.demand_details["temperature_source"] == "weather.selected"

    asyncio.run(scenario())


def test_coordinator_without_heatpump_uses_house_only_forecast():
    async def scenario():
        now = datetime(2026, 11, 10, 6, tzinfo=UTC)
        history = DemandHistory()
        history.observe = observe = MagicMock(wraps=history.observe)
        coordinator = SimpleNamespace(
            _demand_sources=lambda: ["sensor.power", None, "sensor.outdoor"],
            configured_entity_id=lambda key: "weather.selected",
            hass=SimpleNamespace(
                states=SimpleNamespace(
                    get=lambda entity: {
                        "sensor.power": SimpleNamespace(
                            state="2000", attributes={"unit_of_measurement": "W"}
                        ),
                        "sensor.outdoor": SimpleNamespace(
                            state="12.5", attributes={"unit_of_measurement": "°C"}
                        ),
                        "weather.selected": SimpleNamespace(
                            attributes={"temperature": 9.0, "temperature_unit": "°C"}
                        ),
                    }.get(entity)
                ),
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
            _async_demand_weather=AsyncMock(),
        )
        data = {}
        NexusCoordinator._update_demand_forecast(coordinator, data, now)
        observe.assert_called_once_with(
            now, 2000.0, None, 12.5, ZoneInfo("Europe/Amsterdam")
        )
        assert history.wp_configured is False
        assert data["heatpump_forecast"] is None
        # The actual temperature prefers the accurate dedicated sensor.
        assert data["demand_temp_actual"] == 12.5
        assert coordinator.demand_details["temperature_source"] == "sensor.outdoor"
        assert coordinator.demand_details["heatpump_configured"] is False
        assert coordinator.demand_details["weather_entity"] == "weather.selected"

    asyncio.run(scenario())


def _backfill_coordinator(monkeypatch, stats, unit_by_entity):
    """Build a coordinator stub for _async_demand_backfill with mocked recorder."""
    from custom_components.nexus_controller import coordinator as coordinator_module

    monkeypatch.setattr(
        coordinator_module, "statistics_during_period", lambda *args, **kwargs: stats
    )
    monkeypatch.setattr(
        coordinator_module,
        "get_metadata",
        lambda hass, statistic_ids: {
            entity_id: (1, {"unit_of_measurement": unit})
            for entity_id, unit in unit_by_entity.items()
        },
    )
    monkeypatch.setattr(
        coordinator_module,
        "get_instance",
        lambda hass: SimpleNamespace(
            async_add_executor_job=AsyncMock(side_effect=lambda fn, *a: fn(*a))
        ),
    )
    return coordinator_module


def test_startup_backfill_rebuilds_quarters_from_recorder_statistics(monkeypatch):
    base = datetime(2026, 11, 10, 12, tzinfo=UTC)
    stamps = [base + timedelta(minutes=5 * index) for index in range(13)]
    stats = {
        "sensor.power": [{"start": stamp, "mean": 2.0} for stamp in stamps],
        "sensor.heat": [{"start": stamp, "mean": 0.5} for stamp in stamps],
        "sensor.temp": [{"start": stamp, "mean": 41.0} for stamp in stamps],
    }
    units = {"sensor.power": "kW", "sensor.heat": "kW", "sensor.temp": "°C"}
    _backfill_coordinator(monkeypatch, stats, units)
    coordinator = SimpleNamespace(
        _demand_sources=lambda: ["sensor.power", "sensor.heat", "sensor.temp"],
        hass=SimpleNamespace(
            states=SimpleNamespace(
                get=lambda entity: {
                    "sensor.power": SimpleNamespace(state="2.0", attributes={}),
                    "sensor.heat": SimpleNamespace(state="0.5", attributes={}),
                    "sensor.temp": SimpleNamespace(state="20", attributes={}),
                }.get(entity)
            ),
            config=SimpleNamespace(time_zone="Europe/Amsterdam"),
        ),
        _demand_history=DemandHistory(),
    )
    asyncio.run(NexusCoordinator._async_demand_backfill(coordinator))
    history = coordinator._demand_history
    # 12:00 UTC is 13:00 local: slot 52 holds the first backfilled quarter.
    assert history.days["2026-11-10"]["52"] == pytest.approx(1500.0)
    assert history.wp_days["2026-11-10"]["52"] == pytest.approx(500.0)
    assert history.temp_days["2026-11-10"]["52"] == pytest.approx(41.0)
    assert history.wp_configured is True


def test_startup_backfill_accepts_epoch_float_statistic_timestamps(monkeypatch):
    """The recorder stores start_ts as a DOUBLE (epoch seconds), not a datetime."""
    base = datetime(2026, 11, 10, 12, tzinfo=UTC)
    stamps = [base + timedelta(minutes=5 * index) for index in range(13)]
    stats = {
        "sensor.power": [{"start": stamp.timestamp(), "mean": 2.0} for stamp in stamps],
    }
    units = {"sensor.power": "kW"}
    _backfill_coordinator(monkeypatch, stats, units)
    coordinator = SimpleNamespace(
        _demand_sources=lambda: ["sensor.power", None, None],
        hass=SimpleNamespace(
            states=SimpleNamespace(
                get=lambda entity: SimpleNamespace(state="2.0", attributes={})
                if entity == "sensor.power"
                else None
            ),
            config=SimpleNamespace(time_zone="Europe/Amsterdam"),
        ),
        _demand_history=DemandHistory(),
    )
    asyncio.run(NexusCoordinator._async_demand_backfill(coordinator))
    history = coordinator._demand_history
    assert history.days["2026-11-10"]["52"] == pytest.approx(2000.0)
    base = datetime(2026, 11, 10, 12, tzinfo=UTC)
    stamps = [base + timedelta(minutes=5 * index) for index in range(13)]
    stats = {
        "sensor.power": [{"start": stamp, "mean": 1.0} for stamp in stamps],
        "sensor.temp": [{"start": stamp, "mean": 41.0} for stamp in stamps],
    }
    units = {"sensor.power": "kW", "sensor.temp": "°F"}
    _backfill_coordinator(monkeypatch, stats, units)
    coordinator = SimpleNamespace(
        _demand_sources=lambda: ["sensor.power", None, "sensor.temp"],
        hass=SimpleNamespace(
            states=SimpleNamespace(
                get=lambda entity: {
                    "sensor.power": SimpleNamespace(state="1000", attributes={}),
                    "sensor.temp": SimpleNamespace(state="5", attributes={}),
                }.get(entity)
            ),
            config=SimpleNamespace(time_zone="Europe/Amsterdam"),
        ),
        _demand_history=DemandHistory(),
    )
    asyncio.run(NexusCoordinator._async_demand_backfill(coordinator))
    history = coordinator._demand_history
    assert history.days["2026-11-10"]["52"] == pytest.approx(1000.0)
    assert history.temp_days["2026-11-10"]["52"] == pytest.approx(5.0)


def test_startup_backfill_rejects_unsupported_temperature_unit(monkeypatch):
    base = datetime(2026, 11, 10, 12, tzinfo=UTC)
    stamps = [base + timedelta(minutes=5 * index) for index in range(13)]
    stats = {
        "sensor.power": [{"start": stamp, "mean": 1.0} for stamp in stamps],
        "sensor.temp": [{"start": stamp, "mean": 273.0} for stamp in stamps],
    }
    units = {"sensor.power": "kW", "sensor.temp": "K"}
    _backfill_coordinator(monkeypatch, stats, units)
    coordinator = SimpleNamespace(
        _demand_sources=lambda: ["sensor.power", None, "sensor.temp"],
        hass=SimpleNamespace(
            states=SimpleNamespace(
                get=lambda entity: {
                    "sensor.power": SimpleNamespace(state="1000", attributes={}),
                    "sensor.temp": SimpleNamespace(state="273", attributes={}),
                }.get(entity)
            ),
            config=SimpleNamespace(time_zone="Europe/Amsterdam"),
        ),
        _demand_history=DemandHistory(),
    )
    asyncio.run(NexusCoordinator._async_demand_backfill(coordinator))
    history = coordinator._demand_history
    assert history.temp_days == {}
    assert history.days["2026-11-10"]["52"] == pytest.approx(1000.0)


def test_startup_backfill_resolves_renamed_entities_through_the_registry(monkeypatch):
    base = datetime(2026, 11, 10, 12, tzinfo=UTC)
    stamps = [base + timedelta(minutes=5 * index) for index in range(13)]
    stats = {"sensor.current": [{"start": stamp, "mean": 2.0} for stamp in stamps]}
    units = {"sensor.current": "kW"}
    _backfill_coordinator(monkeypatch, stats, units)

    class FakeRegistry:
        def async_get(self, entity_id):
            current = {"sensor.old_name": "sensor.current"}.get(entity_id, entity_id)
            return SimpleNamespace(entity_id=current)

    try:
        from homeassistant.helpers import entity_registry as er_module

        monkeypatch.setattr(
            er_module, "async_get", lambda hass: FakeRegistry()
        )
        coordinator = SimpleNamespace(
            _demand_sources=lambda: ["sensor.old_name", None, None],
            hass=SimpleNamespace(
                states=SimpleNamespace(
                    get=lambda entity: {
                    "sensor.old_name": SimpleNamespace(state="2.0", attributes={}),
                    "sensor.current": SimpleNamespace(state="2.0", attributes={}),
                }.get(entity)
                ),
                config=SimpleNamespace(time_zone="Europe/Amsterdam"),
            ),
            _demand_history=DemandHistory(),
        )
        asyncio.run(NexusCoordinator._async_demand_backfill(coordinator))
        # 2.0 kW mean over a 5-minute sample with no WP configured: 2000 W.
        assert coordinator._demand_history.days["2026-11-10"]["52"] == pytest.approx(2000.0)
    finally:
        monkeypatch.undo()


def test_startup_backfill_retries_when_recorder_is_not_ready_yet(monkeypatch):
    import custom_components.nexus_controller.coordinator as coordinator_module

    base = datetime(2026, 11, 10, 12, tzinfo=UTC)
    stamps = [base + timedelta(minutes=5 * index) for index in range(13)]
    empty: dict = {}
    full = {"sensor.power": [{"start": stamp, "mean": 1.0} for stamp in stamps]}
    queries = [empty, empty, full]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(coordinator_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        coordinator_module, "statistics_during_period", lambda *args, **kwargs: queries.pop(0)
    )
    monkeypatch.setattr(
        coordinator_module,
        "get_metadata",
        lambda hass, statistic_ids: {"sensor.power": (1, {"unit_of_measurement": "kW"})},
    )
    monkeypatch.setattr(
        coordinator_module,
        "get_instance",
        lambda hass: SimpleNamespace(
            async_add_executor_job=AsyncMock(side_effect=lambda fn, *a: fn(*a))
        ),
    )
    coordinator = SimpleNamespace(
        _demand_sources=lambda: ["sensor.power", None, None],
        hass=SimpleNamespace(
            states=SimpleNamespace(
                get=lambda entity: SimpleNamespace(state="1000", attributes={})
                if entity == "sensor.power"
                else None
            ),
            config=SimpleNamespace(time_zone="Europe/Amsterdam"),
        ),
        _demand_history=DemandHistory(),
    )
    asyncio.run(NexusCoordinator._async_demand_backfill(coordinator))
    assert sleeps == [60, 60]
    assert coordinator._demand_history.days["2026-11-10"]["52"] == pytest.approx(1000.0)
