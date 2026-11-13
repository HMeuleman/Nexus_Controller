"""Coordinator for the Nexus controller: reads inputs, plans, tracks, and (optionally) controls."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import replace
from datetime import datetime, timedelta

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .actuator import ActuatorError, BatteryActuator
from .const import (
    BATTERY_CAPACITY_KWH,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_CHARGE_ENABLE,
    CONF_CHARGE_POWER,
    CONF_DISCHARGE_ENABLE,
    CONF_DISCHARGE_POWER,
    CONF_ELECTRICITY_PRICE,
    CONF_FUSE_LIMIT,
    CONF_GRID_POWER_L1,
    CONF_INVERTER_POWER,
    CONF_PRICE_FORECAST,
    CONF_SOLAR_FORECAST,
    CONF_SOLAR_HOURLY_TODAY,
    CONF_SOLAR_HOURLY_TOMORROW,
    DEFAULT_FUSE_LIMIT_A,
    DEFAULT_SAMPLE_INTERVAL_S,
    DOMAIN,
    EXPORT_RESERVE_SOC_PCT,
    INPUT_SENSOR_ENTITIES,
    LEGACY_CHARGE_PRICE_BELOW,
    LEGACY_CYCLE_COST,
    LEGACY_DISCHARGE_PRICE_ABOVE,
    LEGACY_MIN_MARGIN,
    LEGACY_PHASE_HEADROOM,
    MAX_BATTERY_POWER_W,
    ONE_WAY_EFFICIENCY,
    PHASE_POWER_ENTITIES,
    PID_FILTER_ALPHA,
    PID_INTEGRAL_LIMIT_WS,
    PV_POWER_ENTITIES,
    ROUND_TRIP_EFFICIENCY,
    SET_CONTROL_ENABLED,
    SET_DIRECTION_DELAY,
    SET_DISCHARGE_OFFSET,
    SET_DISCHARGE_PRICE_ABOVE,
    SET_EXPORT_INCLUDES_TAX,
    SET_MANUAL_TIMEOUT,
    SET_MAX_CHARGE_PRICE,
    SET_MIN_CONTROL_POWER,
    SET_MIN_DISCHARGE_SOC,
    SET_MIN_EARNINGS,
    SET_PID_KD,
    SET_PID_KI,
    SET_PID_KP,
    SET_PID_START_ERROR,
    SET_PID_STOP_ERROR,
    SET_PLANNER_OVERRIDE,
    SET_POWERSAVE_COOLDOWN,
    SET_POWERSAVE_DURATION,
    SET_POWERSAVE_ENABLED,
    SET_POWERSAVE_INTERVAL,
    SET_POWERSAVE_LEAD,
    SET_POWERSAVE_MIN_SOC,
    SET_RESERVED_ROOM,
    SET_SOLAR_BLOCK,
    SET_TAX,
    SET_WRITEDOWN_RATE,
    SET_WRITEDOWN_SOC,
    SET_WRITEDOWN_WAIT,
    SETTING_DEFAULTS,
    STORAGE_VERSION,
)
from .control import PIDMemory, PIDTuning, simulate_closed_loop_step
from .dispatch import DispatchPlan, DispatchSettings
from .net_charge import ExportSettings, NetChargeSettings
from .powersave import PowerSaveMemory, PowerSaveSettings, decide_powersave, schedule_window
from .quarter_planner import current_plan_dispatch, forecast_quarters_with_current, plan_quarters
from .sample_rate import SampleIntervalEstimator
from .shadow import ShadowSettings, infer_slot_hours, propose_shadow_action
from .solar import daylight_hours, hourly_forecast_quarters, solar_surplus_kwh, update_load_average
from .tracker import BatteryTracker
from .utils import (
    calculate_phase_power_limits,
    parse_energy_kwh,
    parse_forecast_tax,
    parse_numeric_state,
    parse_price_forecast,
    sum_power_states,
    sum_pv_power_states,
)

_LOGGER = logging.getLogger(__name__)

# Reasons whose power comes from the closed loop on the measured grid power.
PID_REASONS = ("high_price_no_solar_reduce_import", "pv_surplus_reduce_export")
# Wait for the other phase sensors of the same meter telegram before acting.
PHASE_SETTLE_S = 0.5
# Gains are defined at this sample interval; faster meters get proportionally smaller steps.
GAIN_REFERENCE_INTERVAL_S = 10.0
HOLD_POWER_W = 50.0
EXPERIMENTAL_PLAN_INTERVAL_S = 300.0
EXPERIMENTAL_PLAN_RETRY_S = 30.0
EXPERIMENTAL_PLAN_MAX_AGE_S = EXPERIMENTAL_PLAN_INTERVAL_S + 60.0
MIN_MEASUREMENT_MAX_AGE_S = 60.0


def _export_reserve_soc(minimum_soc: float, export_includes_tax: bool) -> float:
    """Apply no additional export reserve when export is paid including tax."""
    return minimum_soc if export_includes_tax else max(EXPORT_RESERVE_SOC_PCT, minimum_soc)


class NexusCoordinator(DataUpdateCoordinator[dict[str, float | str | None]]):
    """Read configured HA entities, plan the strategy, track results and optionally control."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=10),
        )
        self.entry = entry
        self.price_forecast: list[tuple[datetime, float]] = []
        self.price_forecast_taxes: dict[datetime, float] = {}
        self.experimental_quarter_plan: dict = {}
        self._quarter_commitment: dict | None = None
        self._quarter_task: asyncio.Task | None = None
        self._quarter_last_start: float | None = None
        self._quarter_refresh_pending = False
        self._planner_startup_ready = False
        self._quarter_status = "unavailable"
        self.settings: dict[str, float | bool] = self._initial_settings()
        self.tracker = BatteryTracker(BATTERY_CAPACITY_KWH)
        self.sample_estimator = SampleIntervalEstimator(DEFAULT_SAMPLE_INTERVAL_S)
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._powersave_store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.powersave"
        )
        started = time.monotonic()
        self._powersave_memory = PowerSaveMemory(started, started, started)
        self._powersave_saved_maintenance = started
        self._powersave_wake_pending = False
        self._powersave_owns_off = False
        self._powersave_measurement_lockout = False
        self.powersave_details: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self._grid_samples = 0
        self._controlled_samples = 0
        self._pid_memory = PIDMemory()
        self._actuator: BatteryActuator | None = None
        self._control_was_on = False
        self._control_status = "disabled"
        self._last_planner_input_warning_signature: tuple | None = None
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._settle_cancel: CALLBACK_TYPE | None = None
        self._last_time: datetime | None = None
        self._manual_mode: str | None = None
        self._manual_until = 0.0

    def _initial_settings(self) -> dict[str, float | bool]:
        """Defaults, seeded from the pre-0.5 config options so upgrades keep their tuning."""
        settings = dict(SETTING_DEFAULTS)
        legacy = {**self.entry.data, **self.entry.options}

        def number(key: str) -> float | None:
            try:
                return float(legacy[key])
            except (KeyError, TypeError, ValueError):
                return None

        for key, legacy_key in (
            (SET_RESERVED_ROOM, LEGACY_PHASE_HEADROOM),
            (SET_MAX_CHARGE_PRICE, LEGACY_CHARGE_PRICE_BELOW),
            (SET_DISCHARGE_PRICE_ABOVE, LEGACY_DISCHARGE_PRICE_ABOVE),
        ):
            if (value := number(legacy_key)) is not None:
                settings[key] = value
        margin, wear = number(LEGACY_MIN_MARGIN), number(LEGACY_CYCLE_COST)
        if margin is not None and wear is not None:
            settings[SET_MIN_EARNINGS] = margin + wear
        return settings

    async def async_setup(self) -> None:
        """Load persisted tracker state and start listening to the grid meter."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self.tracker.state = BatteryTracker.state_from_dict(stored)
        saved = await self._powersave_store.async_load()
        now_s = time.monotonic()
        now_wall = dt_util.utcnow().timestamp()
        try:
            previous = (
                float(saved["maintenance_timestamp"]) if isinstance(saved, dict) else now_wall
            )
            if not math.isfinite(previous) or previous > now_wall:
                previous = now_wall
        except (KeyError, TypeError, ValueError, OverflowError):
            previous = now_wall
        maintenance = now_s + previous - now_wall
        self._powersave_memory = PowerSaveMemory(now_s, now_s, maintenance)
        self._powersave_saved_maintenance = maintenance
        self._powersave_store.async_delay_save(self._powersave_data, 60)
        grid_entity = self.configured_entity_id(CONF_GRID_POWER_L1)
        if grid_entity:
            self._unsubscribers.append(
                async_track_state_change_event(self.hass, [grid_entity], self._on_grid_event)
            )
            self._unsubscribers.append(
                async_track_state_report_event(self.hass, [grid_entity], self._on_grid_event)
            )
        inverter_entity = self.configured_entity_id(CONF_INVERTER_POWER)
        if inverter_entity:
            self._unsubscribers.append(
                async_track_state_change_event(
                    self.hass, [inverter_entity], self._on_inverter_power_event
                )
            )
        battery_entity = self.configured_entity_id(CONF_BATTERY_POWER)
        if battery_entity and inverter_entity:
            self._unsubscribers.append(
                async_track_state_change_event(
                    self.hass, [battery_entity], self._on_powersave_battery_event
                )
            )

    async def async_shutdown(self) -> None:
        """Hand the battery back to the inverter and persist state."""
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._settle_cancel:
            self._settle_cancel()
        self._quarter_refresh_pending = False
        if self._quarter_task is not None and not self._quarter_task.done():
            self._quarter_task.cancel()
            try:
                await self._quarter_task
            except asyncio.CancelledError:
                pass
        if self._control_was_on:
            await self._async_release()
        if self.settings[SET_POWERSAVE_ENABLED] or self._powersave_wake_pending:
            await self._async_set_inverter_power(True)
        await self._powersave_store.async_save(self._powersave_data())
        await self._store.async_save(self.tracker.as_dict())
        await super().async_shutdown()

    async def async_start_planning(self) -> None:
        """Start planning after the settings entities have been restored."""
        self._planner_startup_ready = True
        self._quarter_refresh_pending = True
        await self.async_refresh()

    @callback
    def _on_grid_event(self, event: Event) -> None:
        """Note a new grid sample and, when controlling, schedule a prompt control step."""
        if not self.sample_estimator.observe(time.monotonic()):
            return
        self._grid_samples += 1
        if (
            self.settings[SET_CONTROL_ENABLED] or self._manual_mode
        ) and self._settle_cancel is None:
            self._settle_cancel = async_call_later(self.hass, PHASE_SETTLE_S, self._on_settled)

    @callback
    def _on_settled(self, _now: datetime) -> None:
        self._settle_cancel = None
        self.hass.async_create_task(self.async_refresh())

    @callback
    def _on_inverter_power_event(self, event: Event) -> None:
        state = event.data.get("new_state")
        previous = event.data.get("old_state")
        if state is not None and previous is not None and state.state == previous.state:
            return
        now_s = time.monotonic()
        self._powersave_memory = replace(
            self._powersave_memory,
            on_since_s=now_s if state is not None and state.state == "on" else None,
        )
        if self.settings[SET_POWERSAVE_ENABLED] or self.settings[SET_CONTROL_ENABLED]:
            self.hass.async_create_task(self.async_refresh())

    @callback
    def _on_powersave_battery_event(self, event: Event) -> None:
        state = event.data.get("new_state")
        if state is None:
            return
        power = sum_power_states([(state.state, state.attributes.get("unit_of_measurement"))])
        if power is not None and power != 0:
            self._powersave_memory = replace(
                self._powersave_memory, last_activity_s=time.monotonic()
            )
        if self.settings[SET_POWERSAVE_ENABLED]:
            self.hass.async_create_task(self.async_refresh())

    def set_setting(self, key: str, value: float | bool) -> None:
        """Change a runtime setting (called by the number and switch entities)."""
        if key in (
            SET_MIN_DISCHARGE_SOC,
            SET_MIN_EARNINGS,
            SET_EXPORT_INCLUDES_TAX,
            SET_TAX,
            SET_RESERVED_ROOM,
        ) and self.settings.get(key) != value:
            self._quarter_refresh_pending = True
        self.settings[key] = value

    async def async_set_control(self, enabled: bool) -> None:
        """Turn live control on or off; turning it off releases the battery immediately."""
        self.settings[SET_CONTROL_ENABLED] = enabled
        if enabled:
            self._control_status = "enabled"
            self._pid_memory = PIDMemory()
            self._controlled_samples = self._grid_samples - 1
            await self.async_refresh()
        else:
            self._manual_mode = None
            self._quarter_refresh_pending = False
            await self._async_release()
            self._control_status = "disabled"
            await self.async_refresh()

    async def async_set_planner_override(self, enabled: bool) -> None:
        """Change the planner routing without enabling battery control."""
        was_enabled = bool(self.settings.get(SET_PLANNER_OVERRIDE, False))
        self.settings[SET_PLANNER_OVERRIDE] = enabled
        if enabled and not was_enabled:
            self._quarter_refresh_pending = True
        elif not enabled:
            self._quarter_refresh_pending = False
        self._controlled_samples = self._grid_samples - 1
        await self.async_refresh()

    async def async_set_powersave(self, enabled: bool) -> None:
        """Enable optional inverter powersave without enabling battery control."""
        entity_id = self.configured_entity_id(CONF_INVERTER_POWER)
        if enabled and (not entity_id or not entity_id.startswith("switch.")):
            raise ServiceValidationError(
                "Select an inverter power switch before enabling powersave"
            )
        self.settings[SET_POWERSAVE_ENABLED] = enabled
        self._powersave_owns_off = False
        self._powersave_measurement_lockout = False
        if not enabled:
            self._powersave_wake_pending = True
        await self.async_refresh()

    @property
    def manual_mode(self) -> str | None:
        """The active manual override ("charge" or "discharge"), if any."""
        return self._manual_mode

    async def async_set_manual(self, mode: str, enabled: bool) -> None:
        """Start or stop a time-limited maximum charge/discharge; the modes exclude each other."""
        if enabled:
            if self._manual_mode not in (None, mode):
                raise ServiceValidationError("Stop the other manual override first")
            minutes = float(self.settings[SET_MANUAL_TIMEOUT])
            self._manual_mode = mode
            self._manual_until = time.monotonic() + minutes * 60
            self._pid_memory = PIDMemory()
            self._controlled_samples = self._grid_samples - 1
        elif self._manual_mode == mode:
            await self._async_end_manual("manual_stopped")
        await self.async_refresh()

    async def _async_end_manual(self, reason: str) -> None:
        """Clear the override and hand the battery back unless automatic control is on."""
        self._manual_mode = None
        self._control_status = reason
        if not self.settings[SET_CONTROL_ENABLED]:
            await self._async_release()

    async def _manual_command(
        self, data: dict[str, float | str | None], limits: tuple[float | None, float | None]
    ) -> tuple[DispatchPlan, str]:
        """Maximum power in the chosen direction, within the SOC and phase limits."""
        mode = self._manual_mode
        soc = data.get(CONF_BATTERY_SOC)
        if mode is None or not isinstance(soc, float) or None in limits:
            return DispatchPlan("idle", 0.0, "inputs_unavailable"), "inputs_unavailable"
        if mode == "discharge" and soc <= float(self.settings[SET_MIN_DISCHARGE_SOC]):
            await self._async_end_manual("manual_soc_limit")
            return DispatchPlan("idle", 0.0, "manual_soc_limit"), "manual_soc_limit"
        if mode == "charge" and soc >= 100.0:
            await self._async_end_manual("manual_soc_limit")
            return DispatchPlan("idle", 0.0, "manual_soc_limit"), "manual_soc_limit"
        limit = limits[1] if mode == "discharge" else limits[0]
        power = min(limit or 0.0, MAX_BATTERY_POWER_W)
        return DispatchPlan(mode, power, "manual_override"), "manual_override"  # type: ignore[arg-type]

    async def _async_update_data(self) -> dict[str, float | str | None]:
        async with self._lock:
            return await self._async_compute()

    async def _async_compute(self) -> dict[str, float | str | None]:
        """Read inputs, plan, track earnings and run the control step."""
        data: dict[str, float | str | None] = {}
        for key in INPUT_SENSOR_ENTITIES:
            entity_id = self.configured_entity_id(key)
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

        phases = tuple(data.get(key) for key in PHASE_POWER_ENTITIES)
        battery_power = self._battery_power()
        # Limits come from the load without the battery; its measured effect is removed first.
        base_phases = (
            tuple(phase + battery_power / 3 for phase in phases)  # type: ignore[operator]
            if battery_power is not None and None not in phases
            else phases
        )
        limits = calculate_phase_power_limits(
            base_phases,  # type: ignore[arg-type]
            self._number(CONF_FUSE_LIMIT, DEFAULT_FUSE_LIMIT_A),
            float(self.settings[SET_RESERVED_ROOM]),
            MAX_BATTERY_POWER_W,
        )
        data["max_charge_power"] = limits[0]
        data["max_discharge_power"] = limits[1]

        now = dt_util.utcnow()
        self._read_prices(data, now)
        self._track(data, now, phases, battery_power)
        solar_fill = self._read_solar(data, now)
        data["sample_interval"] = self.sample_estimator.interval_s
        data["discharge_threshold"] = self._discharge_threshold()

        plan = self._plan(data, now, battery_power, solar_fill)
        inverter_ready = await self._async_powersave_step(data, now, phases, battery_power, limits)
        if inverter_ready:
            await self._async_control(data, plan, phases, battery_power, limits)
        else:
            data["control_mode"] = "idle"
            data["control_power"] = 0.0
            data["control_status"] = "inverter_not_ready"
        self._schedule_experimental_quarter_plan(data, now, limits)
        self._store.async_delay_save(self.tracker.as_dict, 60)
        return data

    def _schedule_experimental_quarter_plan(
        self,
        data: dict[str, float | str | None],
        now: datetime,
        limits: tuple[float | None, float | None],
    ) -> None:
        """Schedule diagnostic planning without delaying battery control."""
        data["experimental_quarter_plan"] = self._quarter_status
        if not getattr(self, "_planner_startup_ready", True):
            return
        started = time.monotonic()
        if self._quarter_task is not None and not self._quarter_task.done():
            return
        if (
            not getattr(self, "_quarter_refresh_pending", False)
            and self._quarter_last_start is not None
            and started - self._quarter_last_start
            < (
                EXPERIMENTAL_PLAN_INTERVAL_S
                if self._quarter_status == "experimental"
                else EXPERIMENTAL_PLAN_RETRY_S
            )
        ):
            return
        self._quarter_refresh_pending = False
        self._quarter_last_start = started
        self._quarter_task = self.hass.async_create_task(
            self._async_run_experimental_quarter_plan(data.copy(), now, limits)
        )

        def refresh_when_done(task: asyncio.Task) -> None:
            if not task.cancelled() and getattr(self, "_quarter_refresh_pending", False):
                self.hass.async_create_task(self.async_refresh())

        self._quarter_task.add_done_callback(refresh_when_done)

    async def _async_run_experimental_quarter_plan(
        self,
        data: dict[str, float | str | None],
        now: datetime,
        limits: tuple[float | None, float | None],
    ) -> None:
        """Update the diagnostic snapshot independently of the control cycle."""
        try:
            await self._async_experimental_quarter_plan(data, now, limits)
        except Exception:
            self.experimental_quarter_plan = {}
            self._quarter_status = "unavailable"
            _LOGGER.exception("Experimental quarter plan failed")
        else:
            self._quarter_status = str(data.get("experimental_quarter_plan", "unavailable"))
            if self._quarter_status != "experimental":
                self.experimental_quarter_plan = {}
        if getattr(self, "data", None) is not None and getattr(self, "last_update_success", True):
            self.async_set_updated_data(
                {**self.data, "experimental_quarter_plan": self._quarter_status}
            )

    async def _async_experimental_quarter_plan(
        self,
        data: dict[str, float | str | None],
        now: datetime,
        limits: tuple[float | None, float | None],
    ) -> None:
        """Publish an offline grid-cycle estimate, independent of battery control."""
        data["experimental_quarter_plan"] = "unavailable"
        forecast = forecast_quarters_with_current(self.price_forecast, now)
        soc = data.get(CONF_BATTERY_SOC)
        minimum_soc = float(self.settings[SET_MIN_DISCHARGE_SOC])
        if (
            not forecast
            or not isinstance(soc, (int, float))
        ):
            return
        house_load = self.tracker.state.avg_daytime_load_w
        tax = 0.0 if self.settings[SET_EXPORT_INCLUDES_TAX] else data.get("tax_used")
        if not isinstance(tax, (int, float)) or tax < 0:
            return
        forecast_taxes = [
            0.0
            if self.settings[SET_EXPORT_INCLUDES_TAX]
            else self.price_forecast_taxes.get(start, float(self.settings[SET_TAX]))
            for start, _, _ in forecast
        ]
        solar_quarters = self._experimental_solar_quarters(now)
        solar_values = [
            solar_quarters[start] * duration / 0.25 if start in solar_quarters else None
            for start, _, duration in forecast
        ]
        solar_complete = all(value is not None for value in solar_values)
        solar_input = (
            [value for value in solar_values if value is not None] if solar_complete else None
        )
        min_earnings = float(self.settings[SET_MIN_EARNINGS])
        stored_value = self.tracker.state.stored_value
        export_reserve = _export_reserve_soc(
            minimum_soc, bool(self.settings[SET_EXPORT_INCLUDES_TAX])
        )
        active = None
        if forecast[0][0] <= now:
            previous = getattr(self, "experimental_quarter_plan", {})
            candidates = list(previous.get("slots", []))
            commitment = getattr(self, "_quarter_commitment", None)
            if commitment is not None:
                candidates.append(commitment)
            for slot in candidates:
                try:
                    start = datetime.fromisoformat(slot["forecast_start"])
                    if start.timestamp() == forecast[0][0].timestamp():
                        active = slot.copy()
                        self._quarter_commitment = active.copy()
                        break
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
            if active is not None:
                if (
                    active.get("price_eur_kwh") != forecast[0][1]
                    or active.get("export_tax_eur_kwh") != forecast_taxes[0]
                ):
                    return
        initial_soc = soc
        planning_forecast = forecast
        planning_taxes = forecast_taxes
        planning_solar = solar_input
        if active is not None:
            power = float(active["power_w"])
            duration = forecast[0][2]
            if power > 0:
                if limits[1] is None or not math.isfinite(limits[1]) or limits[1] < 0:
                    return
                power = min(power, limits[1])
                if active.get("discharge_mode") == "nom":
                    solar_power = (solar_values[0] or 0.0) * 1000 / duration
                    power = min(power, max(0.0, (house_load or 0.0) - solar_power))
                reserve = (
                    export_reserve if active.get("discharge_mode") == "export" else minimum_soc
                )
                if soc > reserve:
                    initial_soc = max(
                        reserve,
                        soc - power * duration / 1000 / ONE_WAY_EFFICIENCY
                        / BATTERY_CAPACITY_KWH * 100,
                    )
            elif power < 0:
                if limits[0] is None or not math.isfinite(limits[0]) or limits[0] < 0:
                    return
                power = min(-power, limits[0])
                if active.get("charge_source") == "solar":
                    solar_power = (solar_values[0] or 0.0) * 1000 / duration
                    power = min(power, max(0.0, solar_power - (house_load or 0.0)))
                initial_soc = min(
                    100.0,
                    soc + power * duration / 1000 * ONE_WAY_EFFICIENCY
                    / BATTERY_CAPACITY_KWH * 100,
                )
            planning_forecast = forecast[1:]
            planning_taxes = forecast_taxes[1:]
            planning_solar = solar_input[1:] if solar_input is not None else None
        try:
            result = await self.hass.async_add_executor_job(
                lambda: plan_quarters(
                    [price for _, price, _ in planning_forecast],
                    capacity_kwh=BATTERY_CAPACITY_KWH,
                    initial_soc_pct=initial_soc,
                    min_soc_pct=minimum_soc,
                    max_power_w=MAX_BATTERY_POWER_W,
                    efficiency=ONE_WAY_EFFICIENCY,
                    min_profit_eur_per_kwh=min_earnings,
                    export_tax_eur_per_kwh=tax,
                    house_load_w=house_load or 0.0,
                    initial_value_eur_per_kwh=stored_value,
                    export_taxes_eur_per_kwh=planning_taxes,
                    solar_kwh=planning_solar,
                    durations_hours=[duration for _, _, duration in planning_forecast],
                    export_reserve_soc_pct=export_reserve,
                )
            )
        except ValueError:
            return
        except Exception:
            _LOGGER.exception("Experimental quarter plan failed")
            return
        self.experimental_quarter_plan = {
            "generated_at": now.isoformat(),
            "minimum_earnings_eur_kwh": min_earnings,
            "estimated_profit_eur": round(result.profit_eur, 4),
            "house_load_w": house_load,
            "export_tax_eur_kwh": tax,
            "efficiency_model": "fixed 97% per charge and discharge",
            "planning_power_limit_w": MAX_BATTERY_POWER_W,
            "solar_included": solar_complete,
            "limitations": (
                "Contiguous cycles; bounded inventory levels; constant load; solar omitted "
                "unless forecast is complete; missing forecast tax uses fallback; "
                "estimated profit excludes any retained current slot; "
                "future phase limits may reduce actual power"
            ),
            "slots": [
                {
                    "start": max(start, now).isoformat(),
                    "forecast_start": start.isoformat(),
                    "duration_hours": duration,
                    "price_eur_kwh": price,
                    "export_tax_eur_kwh": slot_tax,
                    "power_w": round(power, 1),
                    "charge_source": charge_source,
                    "soc_pct": round(predicted_soc, 2),
                }
                for (start, price, duration), slot_tax, power, charge_source, predicted_soc in zip(
                    planning_forecast,
                    planning_taxes,
                    result.power_w,
                    result.charge_source,
                    result.soc_pct,
                    strict=True,
                )
            ],
        }
        if active is not None:
            active.update(
                start=now.isoformat(),
                duration_hours=forecast[0][2],
                soc_pct=round(initial_soc, 2),
            )
            self.experimental_quarter_plan["slots"].insert(0, active)
        for slot, solar in zip(self.experimental_quarter_plan["slots"], solar_values, strict=True):
            slot["solar_forecast_kwh"] = solar
            demand = max(
                0.0,
                (house_load or 0.0) - (solar or 0.0) * 1000 / slot["duration_hours"],
            )
            if slot is not active:
                slot["discharge_mode"] = (
                    "nom"
                    if 0 < slot["power_w"] <= demand
                    else "export"
                    if slot["power_w"] > 0
                    else "none"
                )
                if forecast[0][0] <= now:
                    self._quarter_commitment = self.experimental_quarter_plan["slots"][0].copy()
        data["experimental_quarter_plan"] = "experimental"

    def _experimental_solar_quarters(self, now: datetime) -> dict[datetime, float]:
        """Read selected Solar ML hourly forecasts for diagnostics, without guessing entity IDs."""
        timezone = dt_util.get_time_zone(self.hass.config.time_zone)
        if timezone is None:
            return {}
        today = now.astimezone(timezone).date()
        quarters: dict[datetime, float] = {}
        for key, day_offset, attribute in (
            (CONF_SOLAR_HOURLY_TODAY, 0, "hourly_forecast"),
            (CONF_SOLAR_HOURLY_TOMORROW, 1, "hours"),
        ):
            entity_id = self.configured_entity_id(key)
            state = self.hass.states.get(entity_id) if entity_id else None
            if (
                state is None
                or state.state in ("unknown", "unavailable")
                or str(state.attributes.get("unit_of_measurement", "")).casefold() != "kwh"
            ):
                continue
            quarters.update(
                hourly_forecast_quarters(
                    state.attributes.get(attribute),
                    day=today + timedelta(days=day_offset),
                    timezone=timezone,
                )
            )
        return quarters

    def _read_prices(self, data: dict[str, float | str | None], now: datetime) -> None:
        """Parse the forecast and derive the current tax-free price."""
        # Without a dedicated forecast sensor, the price sensor's own attribute is used.
        forecast_entity_id = self.configured_entity_id(
            CONF_PRICE_FORECAST
        ) or self.configured_entity_id(CONF_ELECTRICITY_PRICE)
        forecast_state = self.hass.states.get(forecast_entity_id) if forecast_entity_id else None
        forecast = forecast_state.attributes.get("forecast") if forecast_state is not None else None
        self.price_forecast = parse_price_forecast(forecast)
        taxes = parse_forecast_tax(forecast)
        self.price_forecast_taxes = taxes
        started = [start for start in taxes if start <= now]
        tax = taxes[max(started)] if started else float(self.settings[SET_TAX])
        data["tax_used"] = tax
        price = data.get(CONF_ELECTRICITY_PRICE)
        data["price_tax_free"] = price - tax if isinstance(price, float) else None  # type: ignore[operator]

        upcoming = [price for start, price in self.price_forecast if start > now]
        data["price_forecast_points"] = float(len(upcoming))
        data["forecast_lowest_price"] = min(upcoming) if upcoming else None
        data["forecast_highest_price"] = max(upcoming) if upcoming else None

    def _track(
        self,
        data: dict[str, float | str | None],
        now: datetime,
        phases: tuple,
        battery_power: float | None,
    ) -> None:
        """Advance cycle/earnings tracking and the daytime load average."""
        price = data.get(CONF_ELECTRICITY_PRICE)
        tax = data.get("tax_used")
        grid = sum(phases) if None not in phases else None
        export_price = None
        if isinstance(price, float) and isinstance(tax, float):
            export_price = price if self.settings[SET_EXPORT_INCLUDES_TAX] else price - tax
        self.tracker.update(
            now=now,
            day=dt_util.as_local(now).date().isoformat(),
            soc_pct=data.get(CONF_BATTERY_SOC),  # type: ignore[arg-type]
            battery_power_w=battery_power,
            grid_import_w=grid,
            import_price=price if isinstance(price, float) else None,
            export_price=export_price,
        )
        pv = data.get(PV_POWER_ENTITIES[0])
        if (
            self._last_time is not None
            and grid is not None
            and battery_power is not None
            and pv is not None
        ):
            self.tracker.state.avg_daytime_load_w = update_load_average(
                self.tracker.state.avg_daytime_load_w,
                load_w=grid + pv + battery_power,  # type: ignore[operator]
                pv_power_w=pv,  # type: ignore[arg-type]
                dt_s=(now - self._last_time).total_seconds(),
            )
        self._last_time = now
        state = self.tracker.state
        upcoming = [forecast for start, forecast in self.price_forecast if start > now]
        soc = data.get(CONF_BATTERY_SOC)
        wait_h = float(self.settings[SET_WRITEDOWN_WAIT])
        min_soc = float(self.settings[SET_WRITEDOWN_SOC])
        if self.tracker.write_down(
            now=now,
            soc_pct=soc if isinstance(soc, float) else None,
            forecast_prices=upcoming,
            wait_h=wait_h,
            min_soc_pct=min_soc,
            fraction_per_day=float(self.settings[SET_WRITEDOWN_RATE]) / 100,
        ):
            persistent_notification.async_create(
                self.hass,
                f"The battery has not discharged for over {wait_h:.0f} hours while "
                f"above {min_soc:.0f}% charge, so the value of the stored energy "
                f"is being lowered towards the market price (now {state.stored_value:.3f} "
                "EUR/kWh).",
                title="Nexus Controller",
                notification_id=f"{DOMAIN}_stored_value_writedown",
            )
        data["cycles_today"] = state.cycles_today
        data["cycles_total"] = state.cycles_total
        data["earned_today"] = state.earned_today
        data["avg_daytime_load"] = state.avg_daytime_load_w

    def _read_solar(self, data: dict[str, float | str | None], now: datetime) -> float:
        """Return expected free PV charge in kWh, and publish the surplus estimate."""
        data["solar_surplus_forecast"] = None
        entity_id = self.configured_entity_id(CONF_SOLAR_FORECAST)
        state = self.hass.states.get(entity_id) if entity_id else None
        sun = self.hass.states.get("sun.sun")
        if state is None or sun is None:
            return 0.0
        forecast_kwh = parse_energy_kwh(state.state, state.attributes.get("unit_of_measurement"))
        hours = daylight_hours(
            above_horizon=sun.state == "above_horizon",
            next_rising=dt_util.parse_datetime(str(sun.attributes.get("next_rising"))),
            next_setting=dt_util.parse_datetime(str(sun.attributes.get("next_setting"))),
            now=now,
        )
        surplus = solar_surplus_kwh(forecast_kwh, self.tracker.state.avg_daytime_load_w, hours)
        data["solar_surplus_forecast"] = surplus
        return surplus or 0.0

    def _plan(
        self,
        data: dict[str, float | str | None],
        now: datetime,
        battery_power: float | None,
        solar_fill: float,
    ) -> DispatchPlan:
        """Return the strategy's proposed action and publish it as the shadow proposal."""
        if battery_power is None:
            data["shadow_mode"] = "idle"
            data["shadow_power"] = None
            data["shadow_reason"] = "battery_power_not_configured"
            return DispatchPlan("idle", 0.0, "battery_power_not_configured")

        upcoming_points = [(start, price) for start, price in self.price_forecast if start > now]
        slot_hours = infer_slot_hours([start for start, _ in upcoming_points])
        remaining = 1.0
        if upcoming_points:
            remaining = (upcoming_points[0][0] - now).total_seconds() / (slot_hours * 3600)
        horizon_end = now + timedelta(hours=24)
        phases = tuple(data.get(key) for key in PHASE_POWER_ENTITIES)
        load = sum(phases) + battery_power if None not in phases else None  # type: ignore[arg-type]
        plan = propose_shadow_action(
            phase_imports_w=phases,  # type: ignore[arg-type]
            battery_power_w=battery_power,
            soc_pct=data.get(CONF_BATTERY_SOC),  # type: ignore[arg-type]
            price=data.get(CONF_ELECTRICITY_PRICE),  # type: ignore[arg-type]
            pv_power_w=data.get(PV_POWER_ENTITIES[0]),  # type: ignore[arg-type]
            upcoming_prices=[price for start, price in upcoming_points if start <= horizon_end],
            slot_hours=slot_hours,
            fuse_limit_amps=self._number(CONF_FUSE_LIMIT, DEFAULT_FUSE_LIMIT_A),
            phase_headroom_w=float(self.settings[SET_RESERVED_ROOM]),
            settings=self._shadow_settings(float(data.get("tax_used") or 0.0)),  # type: ignore[arg-type]
            solar_fill_kwh=solar_fill,
            first_slot_remaining=remaining,
            expected_discharge_w=max(load or 0.0, 500.0),
            stored_value_eur_kwh=self.tracker.state.stored_value,
        )
        data["shadow_mode"] = plan.mode
        data["shadow_power"] = plan.power_w
        data["shadow_reason"] = plan.reason
        return plan

    def _discharge_threshold(self) -> float:
        """Price from which stored energy is worth spending: its cost plus desired earnings."""
        threshold = self.tracker.state.stored_value + float(self.settings[SET_MIN_EARNINGS])
        floor = float(self.settings[SET_DISCHARGE_PRICE_ABOVE])
        # Dispatch requires the discharge threshold to stay above the charge threshold.
        return max(threshold, floor, float(self.settings[SET_MAX_CHARGE_PRICE]) + 0.01)

    def _shadow_settings(self, tax: float) -> ShadowSettings:
        """Build strategy settings; min earnings already includes any battery wear."""
        earnings = float(self.settings[SET_MIN_EARNINGS])
        export_tax = 0.0 if self.settings[SET_EXPORT_INCLUDES_TAX] else tax
        return ShadowSettings(
            dispatch=DispatchSettings(
                charge_price_below=float(self.settings[SET_MAX_CHARGE_PRICE]),
                discharge_price_above=self._discharge_threshold(),
                min_soc_pct=float(self.settings[SET_MIN_DISCHARGE_SOC]),
                max_soc_pct=100.0,
                solar_inhibit_discharge_above_w=float(self.settings[SET_SOLAR_BLOCK]),
            ),
            net_charge=NetChargeSettings(earnings, ROUND_TRIP_EFFICIENCY, MAX_BATTERY_POWER_W),
            export=ExportSettings(
                earnings,
                ROUND_TRIP_EFFICIENCY,
                export_tax,
                0.0,
                _export_reserve_soc(
                    float(self.settings[SET_MIN_DISCHARGE_SOC]),
                    bool(self.settings[SET_EXPORT_INCLUDES_TAX]),
                ),
                MAX_BATTERY_POWER_W,
            ),
            capacity_kwh=BATTERY_CAPACITY_KWH,
        )

    def _tuning(self) -> PIDTuning:
        """PID settings; the proportional step shrinks for meters faster than the reference."""
        scale = min(1.0, self.sample_estimator.interval_s / GAIN_REFERENCE_INTERVAL_S)
        kp = float(self.settings[SET_PID_KP]) * scale
        ki = float(self.settings[SET_PID_KI])
        kd = float(self.settings[SET_PID_KD])
        return PIDTuning(
            kp_charge=kp,
            ki_charge=ki,
            kd_charge=kd,
            kp_discharge=kp,
            ki_discharge=ki,
            kd_discharge=kd,
            integral_limit_ws=PID_INTEGRAL_LIMIT_WS,
            filter_alpha=PID_FILTER_ALPHA,
            start_error_w=float(self.settings[SET_PID_START_ERROR]),
            stop_error_w=float(self.settings[SET_PID_STOP_ERROR]),
        )

    async def _async_control(
        self,
        data: dict[str, float | str | None],
        plan: DispatchPlan,
        phases: tuple,
        battery_power: float | None,
        limits: tuple[float | None, float | None],
    ) -> None:
        """Turn the plan into a command when control is enabled and a fresh grid sample exists."""
        data["control_mode"] = "idle"
        data["control_power"] = 0.0
        if self._manual_mode and time.monotonic() >= self._manual_until:
            await self._async_end_manual("manual_timeout")
        if not self.settings[SET_CONTROL_ENABLED] and self._manual_mode is None:
            data["control_status"] = self._control_status if self._control_was_on else "disabled"
            return
        planner_active = self._manual_mode is None and self.settings.get(
            SET_PLANNER_OVERRIDE, False
        )
        watchdog_plan = None
        if self._controlled_samples == self._grid_samples:
            if planner_active and self._control_was_on:
                watchdog_plan = self._planner_action(
                    data, dt_util.utcnow(), phases, battery_power, limits
                )
            if watchdog_plan is None or watchdog_plan.mode != "idle":
                previous_data = getattr(self, "data", None)
                if self._control_was_on and isinstance(previous_data, dict):
                    data["control_mode"] = previous_data.get("control_mode", "idle")
                    data["control_power"] = previous_data.get("control_power", 0.0)
                data["control_status"] = self._control_status
                return
        self._controlled_samples = self._grid_samples
        self._control_was_on = True

        if planner_active:
            plan = watchdog_plan or self._planner_action(
                data, dt_util.utcnow(), phases, battery_power, limits
            )
        command = plan
        status = "ok"
        if self._manual_mode:
            command, status = await self._manual_command(data, limits)
        elif battery_power is None or None in phases or limits[0] is None or limits[1] is None:
            command, status = DispatchPlan("idle", 0.0, "inputs_unavailable"), "inputs_unavailable"
        elif plan.reason in PID_REASONS:
            # Load without the battery: below the minimum there is nothing worth regulating.
            base_net = sum(phases) + battery_power
            offset = float(self.settings[SET_DISCHARGE_OFFSET])
            # The setpoint shift below would otherwise exceed the load that is being regulated.
            minimum = max(float(self.settings[SET_MIN_CONTROL_POWER]), offset)
            if (plan.mode == "discharge" and base_net < minimum) or (
                plan.mode == "charge" and base_net > -minimum
            ):
                self._pid_memory = PIDMemory()
                command = DispatchPlan("idle", 0.0, "below_min_control_power")
                status = command.reason
            else:
                # A closed loop cancels any added output, so the offset shifts the setpoint:
                # discharge settles at this much export, charge at this much import.
                shift = offset if plan.mode == "discharge" else -offset
                pid_limits = limits
                if planner_active:
                    pid_limits = (
                        min(limits[0], plan.power_w) if plan.mode == "charge" else limits[0],
                        min(limits[1], plan.power_w) if plan.mode == "discharge" else limits[1],
                    )
                command = self._pid_command(plan, sum(phases) + shift, battery_power, pid_limits)
                if planner_active and command.mode == plan.mode:
                    command = DispatchPlan(
                        command.mode, min(command.power_w, plan.power_w), command.reason
                    )
                status = command.reason
        else:
            self._pid_memory = PIDMemory()
            if plan.mode == "idle":
                status = plan.reason

        if status == "planned_inputs_unavailable":
            log_failure = getattr(self, "_log_planner_input_failure", None)
            if log_failure is not None:
                log_failure(dt_util.utcnow(), data, phases, battery_power, limits)
        else:
            if hasattr(self, "_last_planner_input_warning_signature"):
                self._last_planner_input_warning_signature = None

        try:
            actuator = self._get_actuator()
            applied = await actuator.async_apply(
                command.mode,
                command.power_w,
                min_interval_s=self.sample_estimator.interval_s,
                direction_delay_s=float(self.settings[SET_DIRECTION_DELAY]),
            )
        except ActuatorError as err:
            _LOGGER.error("Battery control stopped: %s", err)
            self.settings[SET_CONTROL_ENABLED] = False
            self.settings[SET_PLANNER_OVERRIDE] = False
            self._quarter_refresh_pending = False
            self._manual_mode = None
            await self._async_release()
            self._control_status = f"error: {err}"
            data["control_status"] = self._control_status
            return
        self._control_status = f"{status}/{applied}"
        data["control_mode"] = command.mode
        data["control_power"] = command.power_w
        data["control_status"] = self._control_status

    def _pid_command(
        self,
        plan: DispatchPlan,
        net_import_w: float,
        battery_power: float,
        limits: tuple[float | None, float | None],
    ) -> DispatchPlan:
        """Run the closed loop on the measured grid power for NOM-style modes."""
        charge, discharge = max(-battery_power, 0.0), max(battery_power, 0.0)
        tuning = self._tuning()
        result = simulate_closed_loop_step(
            net_import_w=net_import_w,
            actual_charge_power_w=charge,
            actual_discharge_power_w=discharge,
            permitted_mode=plan.mode,
            max_charge_power_w=limits[0],
            max_discharge_power_w=limits[1],
            interval_seconds=self.sample_estimator.interval_s,
            tuning=tuning,
            memory=self._pid_memory,
        )
        self._pid_memory = result.memory
        command = result.plan
        if command.mode != "idle":
            return command

        # Inside the deadband, or just overshot: ease off or hold instead of dropping to zero.
        active = discharge if plan.mode == "discharge" else charge
        if active < HOLD_POWER_W:
            return command
        if command.reason == "hysteresis_deadband":
            return DispatchPlan(plan.mode, active, "deadband_hold")
        if command.reason == "power_direction_conflicts_with_permission":
            direction = 1 if plan.mode == "discharge" else -1
            eased = max(active + tuning.kp_discharge * net_import_w * direction, 0.0)
            return DispatchPlan(plan.mode if eased > 0 else "idle", eased, "overshoot_ease")
        return command

    def _get_actuator(self) -> BatteryActuator:
        if self._actuator is None:
            ids = [
                self.configured_entity_id(key)
                for key in (
                    CONF_CHARGE_ENABLE,
                    CONF_CHARGE_POWER,
                    CONF_DISCHARGE_ENABLE,
                    CONF_DISCHARGE_POWER,
                )
            ]
            if not all(ids):
                raise ActuatorError("charge/discharge control entities are not configured")
            self._actuator = BatteryActuator(self.hass, *ids)  # type: ignore[arg-type]
        return self._actuator

    async def _async_release(self) -> None:
        """Turn forced charge/discharge off, logging instead of raising."""
        self._control_was_on = False
        self._pid_memory = PIDMemory()
        try:
            await self._get_actuator().async_release()
        except ActuatorError as err:
            _LOGGER.error("Releasing the battery failed: %s", err)

    def _battery_power(self) -> float | None:
        """Measured net battery power in W (positive discharges), if a sensor is configured."""
        entity_id = self.configured_entity_id(CONF_BATTERY_POWER)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None:
            return None
        return sum_power_states([(state.state, state.attributes.get("unit_of_measurement"))])

    def _powersave_data(self) -> dict[str, float]:
        return {
            "maintenance_timestamp": dt_util.utcnow().timestamp()
            + self._powersave_memory.maintenance_s
            - time.monotonic()
        }

    async def _async_set_inverter_power(self, on: bool) -> bool:
        entity_id = self.configured_entity_id(CONF_INVERTER_POWER)
        if not entity_id or not entity_id.startswith("switch."):
            return False
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        if state.state == ("on" if on else "off"):
            return True
        try:
            await self.hass.services.async_call(
                "switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True
            )
        except Exception as err:
            _LOGGER.error("Setting inverter power failed: %s", err)
            return False
        return True

    async def _async_powersave_step(
        self,
        data: dict[str, float | str | None],
        now: datetime,
        phases: tuple,
        battery_power: float | None,
        limits: tuple[float | None, float | None],
    ) -> bool:
        """Apply an opted-in powersave decision and gate commands on confirmed warmup."""
        entity_id = self.configured_entity_id(CONF_INVERTER_POWER)
        if not entity_id:
            data["powersave_status"] = "not_configured"
            self.powersave_details = {"configured": False}
            return True
        state = self.hass.states.get(entity_id)
        inverter_on = (
            state.state == "on" if state is not None and state.state in ("on", "off") else None
        )
        enabled = bool(self.settings[SET_POWERSAVE_ENABLED])
        planner_enabled = bool(
            self.settings[SET_PLANNER_OVERRIDE] and self.settings[SET_CONTROL_ENABLED]
        )
        now_s = time.monotonic()
        window = None
        ages = self._planner_measurement_ages(now)
        interval = self.sample_estimator.interval_s
        max_measurement_age = (
            max(MIN_MEASUREMENT_MAX_AGE_S, interval * 3)
            if math.isfinite(interval) and interval > 0
            else None
        )
        readings_fresh = (
            ages is not None
            and max_measurement_age is not None
            and all(0 <= age <= max_measurement_age for age in ages)
        )
        plan_earnings = self.experimental_quarter_plan.get("minimum_earnings_eur_kwh")
        if (
            enabled
            and planner_enabled
            and readings_fresh
            and isinstance(plan_earnings, (int, float))
            and math.isfinite(plan_earnings)
            and plan_earnings == self.settings[SET_MIN_EARNINGS]
        ):
            action = self._planner_action(data, now, phases, battery_power, limits)
            if action.reason not in (
                "planner_unavailable_or_stale",
                "planner_price_mismatch",
                "planned_inputs_unavailable",
            ):
                window = schedule_window(self.experimental_quarter_plan.get("slots"), now)
        plan_end = now_s + window[0] - now.timestamp() if window is not None else None
        next_work = (
            now_s + window[1] - now.timestamp()
            if window is not None and window[1] is not None
            else None
        )
        try:
            settings = PowerSaveSettings(
                cooldown_s=float(self.settings[SET_POWERSAVE_COOLDOWN]) * 60,
                lead_s=max(float(self.settings[SET_POWERSAVE_LEAD]) * 60, 60.0) + 10,
                maintenance_interval_s=float(self.settings[SET_POWERSAVE_INTERVAL]) * 3600 - 10,
                maintenance_duration_s=float(self.settings[SET_POWERSAVE_DURATION]) * 60,
                min_soc_pct=float(self.settings[SET_POWERSAVE_MIN_SOC]),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            data["powersave_status"] = "invalid_settings_or_clock"
            self.powersave_details = {"configured": True, "enabled": enabled}
            if enabled or self._powersave_wake_pending:
                await self._async_set_inverter_power(True)
            return False
        decision = decide_powersave(
            now_s=now_s,
            inverter_on=inverter_on,
            enabled=enabled,
            planner_enabled=planner_enabled,
            plan_valid=window is not None,
            schedule_end_s=plan_end,
            next_work_s=next_work,
            soc_pct=data.get(CONF_BATTERY_SOC),  # type: ignore[arg-type]
            battery_power_w=battery_power,
            readings_fresh=readings_fresh,
            manual_active=self._manual_mode is not None,
            memory=self._powersave_memory,
            settings=settings,
        )
        self._powersave_memory = decision.memory
        data["powersave_status"] = decision.reason
        self.powersave_details = {
            "configured": True,
            "enabled": enabled,
            "inverter_power": (
                "on" if inverter_on is True else "off" if inverter_on is False else "unknown"
            ),
            "requested_power": decision.power if enabled or self._powersave_wake_pending else None,
            "measurements_fresh": readings_fresh,
            "measurement_max_age_s": max_measurement_age,
            "measurements": self._planner_measurement_diagnostics(now, max_measurement_age),
            "measurement_lockout": getattr(self, "_powersave_measurement_lockout", False),
        }
        if decision.reason != "invalid_settings_or_clock":
            def at(value: float | None) -> str | None:
                if value is None or not math.isfinite(value):
                    return None
                try:
                    return (now + timedelta(seconds=value - now_s)).isoformat()
                except OverflowError:
                    return None

            self.powersave_details.update(
                inverter_on_since=at(decision.memory.on_since_s),
                last_unavailable_at=at(decision.memory.last_unavailable_s),
                cooldown_until=at(
                    max(
                        decision.memory.started_s,
                        decision.memory.last_activity_s,
                        decision.memory.on_since_s
                        if decision.memory.on_since_s is not None
                        else decision.memory.started_s,
                        decision.memory.last_unavailable_s
                        if decision.memory.last_unavailable_s is not None
                        else decision.memory.started_s,
                    )
                    + settings.cooldown_s
                ),
                control_ready_at=at(
                    decision.memory.on_since_s + settings.warmup_s
                    if decision.memory.on_since_s is not None
                    else None
                ),
                next_planned_work_at=at(next_work),
                planned_wake_at=at(next_work - settings.lead_s if next_work is not None else None),
                maintenance_start_at=at(
                    decision.memory.maintenance_s
                    + settings.maintenance_interval_s
                    - settings.maintenance_duration_s
                ),
            )
        if decision.memory.maintenance_s - self._powersave_saved_maintenance >= 60:
            self._powersave_saved_maintenance = decision.memory.maintenance_s
            self._powersave_store.async_delay_save(self._powersave_data, 60)
        if (
            enabled
            and inverter_on is False
            and decision.reason == "measurements_unavailable"
            and getattr(self, "_powersave_owns_off", False)
        ):
            measurements = self.powersave_details.get("measurements", {})
            failed_measurements = {
                key: details
                for key, details in measurements.items()
                if not details.get("fresh", False)
            }
            _LOGGER.error(
                "Powersave measurement lockout: inverter was off under Nexus control; "
                "max_age_s=%s failed_measurements=%s soc_pct=%r battery_power_w=%r",
                max_measurement_age,
                failed_measurements or "none reported; inspect measurement values",
                data.get(CONF_BATTERY_SOC),
                battery_power,
            )
            self.settings[SET_POWERSAVE_ENABLED] = False
            self._powersave_wake_pending = True
            self._powersave_owns_off = False
            self._powersave_measurement_lockout = True
            data["powersave_status"] = "measurements_lockout"
            self.powersave_details.update(
                enabled=False, measurement_lockout=True, requested_power="on"
            )
        elif getattr(self, "_powersave_measurement_lockout", False) and not enabled:
            data["powersave_status"] = "measurements_lockout"
        if self._powersave_wake_pending and inverter_on:
            self._powersave_wake_pending = False
        if (enabled or self._powersave_wake_pending) and decision.power == "on":
            if inverter_on is not True:
                if not await self._async_set_inverter_power(True):
                    data["powersave_status"] = "inverter_write_failed"
            else:
                self._powersave_owns_off = False
        elif enabled and decision.power == "off" and inverter_on is True:
            force_states = [
                self.hass.states.get(self.configured_entity_id(key))
                for key in (CONF_CHARGE_ENABLE, CONF_DISCHARGE_ENABLE)
            ]
            if all(force is not None and force.state == "off" for force in force_states):
                self._powersave_memory = replace(self._powersave_memory, on_since_s=None)
                if await self._async_set_inverter_power(False):
                    self._powersave_owns_off = True
                else:
                    data["powersave_status"] = "inverter_write_failed"
                return False
            data["powersave_status"] = "waiting_for_forced_modes_to_stop"
            return bool(
                decision.memory.on_since_s is not None
                and now_s - decision.memory.on_since_s >= settings.warmup_s
            )
        return bool(inverter_on and decision.ready_for_control)

    def _planner_measurement_diagnostics(
        self, now: datetime, max_age_s: float | None
    ) -> dict[str, dict[str, object]]:
        """Describe the actual configured reports without relaxing freshness checks."""
        details = {}
        for key in (*PHASE_POWER_ENTITIES, CONF_BATTERY_SOC, CONF_BATTERY_POWER):
            entity_id = self.configured_entity_id(key)
            state = self.hass.states.get(entity_id) if entity_id else None
            reported = (
                getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
                if state is not None
                else None
            )
            age = (
                now.timestamp() - reported.timestamp()
                if isinstance(reported, datetime) and reported.tzinfo is not None
                else None
            )
            details[key] = {
                "entity_id": entity_id,
                "state": state.state if state is not None else None,
                "age_s": round(age, 2) if age is not None else None,
                "fresh": (
                    state is not None
                    and state.state not in ("unknown", "unavailable")
                    and age is not None
                    and max_age_s is not None
                    and 0 <= age <= max_age_s
                ),
            }
        return details

    def _log_planner_input_failure(
        self,
        now: datetime,
        data: dict[str, float | str | None],
        phases: tuple,
        battery_power: float | None,
        limits: tuple[float | None, float | None],
    ) -> None:
        """Log the measurement snapshot once per distinct planner-input failure."""
        interval = self.sample_estimator.interval_s
        max_age_s = (
            max(MIN_MEASUREMENT_MAX_AGE_S, interval * 3)
            if math.isfinite(interval) and interval > 0
            else None
        )
        measurements = self._planner_measurement_diagnostics(now, max_age_s)
        failed = {
            key: details for key, details in measurements.items() if not details["fresh"]
        }
        signature = tuple(
            (key, str(details["state"])) for key, details in failed.items()
        ) or (("planner_values", "unavailable_or_invalid"),)
        if signature == self._last_planner_input_warning_signature:
            return
        self._last_planner_input_warning_signature = signature
        _LOGGER.warning(
            "Quarter-plan inputs unavailable; max_age_s=%s failed_measurements=%s "
            "dispatch_values=%s",
            max_age_s,
            failed or "none; one or more planner values or slot fields are invalid",
            {
                "phases_w": phases,
                "soc_pct": data.get(CONF_BATTERY_SOC),
                "battery_power_w": battery_power,
                "pv_power_w": data.get(PV_POWER_ENTITIES[0]),
                "limits_w": limits,
            },
        )

    def _planner_measurement_ages(self, now: datetime) -> tuple[float, ...] | None:
        """Return ages of the configured phase, SOC and battery power reports."""
        ages = []
        for key in (*PHASE_POWER_ENTITIES, CONF_BATTERY_SOC, CONF_BATTERY_POWER):
            entity_id = self.configured_entity_id(key)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or state.state in ("unknown", "unavailable"):
                return None
            reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
            if not isinstance(reported, datetime) or reported.tzinfo is None:
                return None
            ages.append(now.timestamp() - reported.timestamp())
        return tuple(ages)

    def _planner_action(
        self,
        data: dict[str, float | str | None],
        now: datetime,
        phases: tuple,
        battery_power: float | None,
        limits: tuple[float | None, float | None],
    ) -> DispatchPlan:
        """Validate the current experimental plan against live inputs without writing."""
        idle = DispatchPlan("idle", 0.0, "planner_unavailable_or_stale")
        if self._quarter_status != "experimental" or now.tzinfo is None:
            return idle
        try:
            created = datetime.fromisoformat(self.experimental_quarter_plan["generated_at"])
            age = now.timestamp() - created.timestamp()
            if created.tzinfo is None or not 0 <= age <= EXPERIMENTAL_PLAN_MAX_AGE_S:
                return idle
            planned_earnings = float(self.experimental_quarter_plan["minimum_earnings_eur_kwh"])
            current_earnings = float(self.settings[SET_MIN_EARNINGS])
            if not math.isfinite(planned_earnings) or not math.isfinite(current_earnings):
                return idle
            slots = self.experimental_quarter_plan["slots"]
            current = None
            observed_forecast = {start.timestamp(): price for start, price in self.price_forecast}
            for slot in slots:
                tariff_start = datetime.fromisoformat(slot["forecast_start"])
                if tariff_start.tzinfo is None:
                    return idle
                if tariff_start.timestamp() + 900 > now.timestamp():
                    planned_quote = float(slot["price_eur_kwh"])
                    observed_quote = observed_forecast.get(tariff_start.timestamp())
                    planned_tax = float(slot["export_tax_eur_kwh"])
                    current_tax = (
                        0.0
                        if self.settings[SET_EXPORT_INCLUDES_TAX]
                        else self.price_forecast_taxes.get(
                            tariff_start, float(self.settings[SET_TAX])
                        )
                    )
                    if (
                        not math.isfinite(planned_quote)
                        or not math.isfinite(planned_tax)
                        or not math.isfinite(current_tax)
                        or abs(current_tax - planned_tax) > 1e-6
                        or observed_quote is None
                        or not math.isfinite(observed_quote)
                        or abs(observed_quote - planned_quote) > 1e-6
                    ):
                        return idle
                start = datetime.fromisoformat(slot["start"])
                end = start.timestamp() + float(slot["duration_hours"]) * 3600
                if start.tzinfo is not None and start.timestamp() <= now.timestamp() < end:
                    current = slot
            if current is None:
                return idle
            if planned_earnings != current_earnings:
                commitment = getattr(self, "_quarter_commitment", None)
                if not isinstance(commitment, dict) or any(
                    current.get(key) != commitment.get(key)
                    for key in ("forecast_start", "power_w", "charge_source", "discharge_mode")
                ):
                    return idle
            forecast_start = datetime.fromisoformat(current["forecast_start"])
            quote = float(current["price_eur_kwh"])
            current_price = float(data[CONF_ELECTRICITY_PRICE])
            matching_price = next(
                (
                    price
                    for start, price in self.price_forecast
                    if start.timestamp() == forecast_start.timestamp()
                ),
                None,
            )
            if (
                forecast_start.tzinfo is None
                or not forecast_start.timestamp()
                <= now.timestamp()
                < forecast_start.timestamp() + 900
                or not math.isfinite(quote)
                or not math.isfinite(current_price)
                or matching_price is None
                or abs(matching_price - quote) > 1e-6
            ):
                return idle
            if abs(current_price - quote) > 1e-6:
                return DispatchPlan("idle", 0.0, "planner_price_mismatch")
            interval = float(self.sample_estimator.interval_s)
            if not math.isfinite(interval) or interval <= 0:
                return idle
            minimum_soc = float(self.settings[SET_MIN_DISCHARGE_SOC])
            return current_plan_dispatch(
                current,
                now=now,
                soc_pct=data.get(CONF_BATTERY_SOC),  # type: ignore[arg-type]
                min_soc_pct=minimum_soc,
                export_reserve_soc_pct=_export_reserve_soc(
                    minimum_soc, bool(self.settings[SET_EXPORT_INCLUDES_TAX])
                ),
                max_charge_w=limits[0],
                max_discharge_w=limits[1],
                grid_import_w=sum(phases) if None not in phases else None,
                battery_power_w=battery_power,
                pv_power_w=data.get(PV_POWER_ENTITIES[0]),  # type: ignore[arg-type]
                measurement_ages_s=self._planner_measurement_ages(now),
                max_measurement_age_s=max(MIN_MEASUREMENT_MAX_AGE_S, interval * 3),
            )
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            return idle

    def _number(self, key: str, default: float) -> float:
        """Return a configured numeric option or its safe default."""
        value = self.entry.options.get(key, self.entry.data.get(key, default))
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def configured_entity_id(self, key: str) -> str | None:
        """Return the configured entity ID for a mapping key."""
        return self.entry.options.get(key, self.entry.data.get(key))
