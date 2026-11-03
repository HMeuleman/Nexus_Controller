"""Coordinator for the Nexus controller: reads inputs, plans, tracks, and (optionally) controls."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
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
    CONF_PRICE_FORECAST,
    CONF_SOLAR_FORECAST,
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
    MIN_SOC_PCT,
    PHASE_POWER_ENTITIES,
    PID_FILTER_ALPHA,
    PID_INTEGRAL_LIMIT_WS,
    PV_POWER_ENTITIES,
    ROUND_TRIP_EFFICIENCY,
    SET_CONTROL_ENABLED,
    SET_DISCHARGE_PRICE_ABOVE,
    SET_EXPORT_INCLUDES_TAX,
    SET_MAX_CHARGE_PRICE,
    SET_MIN_EARNINGS,
    SET_PID_KD,
    SET_PID_KI,
    SET_PID_KP,
    SET_PID_START_ERROR,
    SET_PID_STOP_ERROR,
    SET_RESERVED_ROOM,
    SET_TAX,
    SETTING_DEFAULTS,
    STORAGE_VERSION,
)
from .control import PIDMemory, PIDTuning, simulate_closed_loop_step
from .dispatch import DispatchPlan, DispatchSettings
from .net_charge import ExportSettings, NetChargeSettings
from .sample_rate import SampleIntervalEstimator
from .shadow import ShadowSettings, infer_slot_hours, propose_shadow_action
from .solar import daylight_hours, solar_surplus_kwh, update_load_average
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
        self.settings: dict[str, float | bool] = self._initial_settings()
        self.tracker = BatteryTracker(BATTERY_CAPACITY_KWH)
        self.sample_estimator = SampleIntervalEstimator(DEFAULT_SAMPLE_INTERVAL_S)
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._lock = asyncio.Lock()
        self._grid_samples = 0
        self._controlled_samples = 0
        self._pid_memory = PIDMemory()
        self._actuator: BatteryActuator | None = None
        self._control_was_on = False
        self._control_status = "disabled"
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._settle_cancel: CALLBACK_TYPE | None = None
        self._last_time: datetime | None = None

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
        grid_entity = self.configured_entity_id(CONF_GRID_POWER_L1)
        if grid_entity:
            self._unsubscribers.append(
                async_track_state_change_event(self.hass, [grid_entity], self._on_grid_event)
            )
            self._unsubscribers.append(
                async_track_state_report_event(self.hass, [grid_entity], self._on_grid_event)
            )

    async def async_shutdown(self) -> None:
        """Hand the battery back to the inverter and persist state."""
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._settle_cancel:
            self._settle_cancel()
        if self._control_was_on:
            await self._async_release()
        await self._store.async_save(self.tracker.as_dict())
        await super().async_shutdown()

    @callback
    def _on_grid_event(self, event: Event) -> None:
        """Note a new grid sample and, when controlling, schedule a prompt control step."""
        if not self.sample_estimator.observe(time.monotonic()):
            return
        self._grid_samples += 1
        if self.settings[SET_CONTROL_ENABLED] and self._settle_cancel is None:
            self._settle_cancel = async_call_later(self.hass, PHASE_SETTLE_S, self._on_settled)

    @callback
    def _on_settled(self, _now: datetime) -> None:
        self._settle_cancel = None
        self.hass.async_create_task(self.async_refresh())

    def set_setting(self, key: str, value: float | bool) -> None:
        """Change a runtime setting (called by the number and switch entities)."""
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
            await self._async_release()
            self._control_status = "disabled"
            await self.async_refresh()

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
        await self._async_control(data, plan, phases, battery_power, limits)
        self._store.async_delay_save(self.tracker.as_dict, 60)
        return data

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
                min_soc_pct=MIN_SOC_PCT,
                max_soc_pct=100.0,
            ),
            net_charge=NetChargeSettings(earnings, ROUND_TRIP_EFFICIENCY, MAX_BATTERY_POWER_W),
            export=ExportSettings(
                earnings,
                ROUND_TRIP_EFFICIENCY,
                export_tax,
                0.0,
                EXPORT_RESERVE_SOC_PCT,
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
        if not self.settings[SET_CONTROL_ENABLED]:
            data["control_status"] = "disabled"
            return
        if self._controlled_samples == self._grid_samples:
            data["control_status"] = self._control_status
            return
        self._controlled_samples = self._grid_samples
        self._control_was_on = True

        command = plan
        status = "ok"
        if battery_power is None or None in phases or limits[0] is None or limits[1] is None:
            command, status = DispatchPlan("idle", 0.0, "inputs_unavailable"), "inputs_unavailable"
        elif plan.reason in PID_REASONS:
            command = self._pid_command(plan, sum(phases), battery_power, limits)
            status = command.reason
        else:
            self._pid_memory = PIDMemory()
            if plan.mode == "idle":
                status = plan.reason

        try:
            actuator = self._get_actuator()
            applied = await actuator.async_apply(
                command.mode,
                command.power_w,
                min_interval_s=self.sample_estimator.interval_s,
            )
        except ActuatorError as err:
            _LOGGER.error("Battery control stopped: %s", err)
            self.settings[SET_CONTROL_ENABLED] = False
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
