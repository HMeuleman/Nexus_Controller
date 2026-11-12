"""Offline decision logic for optional inverter powersave."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime


@dataclass(frozen=True)
class PowerSaveSettings:
    cooldown_s: float = 900.0
    lead_s: float = 300.0
    maintenance_interval_s: float = 86400.0
    maintenance_duration_s: float = 1800.0
    min_soc_pct: float = 8.0
    min_off_s: float = 900.0
    warmup_s: float = 60.0


@dataclass(frozen=True)
class PowerSaveMemory:
    started_s: float
    last_activity_s: float
    maintenance_s: float
    on_since_s: float | None = None
    last_unavailable_s: float | None = None


@dataclass(frozen=True)
class PowerSaveDecision:
    power: str
    reason: str
    ready_for_control: bool
    memory: PowerSaveMemory


def schedule_window(slots: object, now: datetime) -> tuple[float, float | None] | None:
    """Return the covered end and next active start, rejecting gaps and malformed slots."""
    if now.tzinfo is None or not isinstance(slots, list) or not slots:
        return None
    current_time = now.timestamp()
    previous_end = None
    next_work = None
    covered = False
    try:
        for slot in slots:
            start = datetime.fromisoformat(slot["start"])
            if start.tzinfo is None:
                return None
            start_time = start.timestamp()
            duration = float(slot["duration_hours"]) * 3600
            power = float(slot["power_w"])
            if (
                not all(math.isfinite(value) for value in (start_time, duration, power))
                or not 0 < duration <= 900
                or (previous_end is not None and abs(start_time - previous_end) > 0.01)
            ):
                return None
            end = start_time + duration
            if start_time <= current_time < end:
                covered = True
            if end > current_time and power != 0 and next_work is None:
                next_work = max(start_time, current_time)
            previous_end = end
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return None
    return (previous_end, next_work) if covered and previous_end is not None else None


def decide_powersave(
    *,
    now_s: float,
    inverter_on: bool | None,
    enabled: bool,
    planner_enabled: bool,
    plan_valid: bool,
    schedule_end_s: float | None,
    next_work_s: float | None,
    soc_pct: float | None,
    battery_power_w: float | None,
    readings_fresh: bool,
    manual_active: bool,
    memory: PowerSaveMemory,
    settings: PowerSaveSettings,
) -> PowerSaveDecision:
    """Decide a power request from validated inputs without writing to the inverter."""
    values = (
        now_s,
        memory.started_s,
        memory.last_activity_s,
        memory.maintenance_s,
        settings.cooldown_s,
        settings.lead_s,
        settings.maintenance_interval_s,
        settings.maintenance_duration_s,
        settings.min_soc_pct,
        settings.min_off_s,
        settings.warmup_s,
        *(value for value in (memory.last_unavailable_s,) if value is not None),
    )
    if (
        not all(math.isfinite(value) for value in values)
        or min(settings.cooldown_s, settings.lead_s, settings.min_off_s, settings.warmup_s) < 0
        or settings.maintenance_interval_s <= 0
        or not 0 < settings.maintenance_duration_s < settings.maintenance_interval_s
        or not 0 <= settings.min_soc_pct <= 100
        or now_s
        < max(
            memory.started_s,
            memory.last_activity_s,
            memory.maintenance_s,
            memory.last_unavailable_s
            if memory.last_unavailable_s is not None
            else memory.started_s,
        )
    ):
        return PowerSaveDecision("on", "invalid_settings_or_clock", False, memory)
    on_since = memory.on_since_s if inverter_on else None
    if inverter_on and (on_since is None or not math.isfinite(on_since) or on_since > now_s):
        on_since = now_s
    last_activity = memory.last_activity_s
    if battery_power_w is not None and math.isfinite(battery_power_w) and battery_power_w != 0:
        last_activity = now_s
    maintenance = memory.maintenance_s
    if on_since is not None and now_s - on_since >= settings.maintenance_duration_s:
        maintenance = now_s
    memory = PowerSaveMemory(
        memory.started_s, last_activity, maintenance, on_since, memory.last_unavailable_s
    )
    ready = on_since is not None and now_s - on_since >= settings.warmup_s

    def on(reason: str) -> PowerSaveDecision:
        updated = (
            replace(memory, last_unavailable_s=now_s)
            if reason in ("measurements_unavailable", "plan_unavailable", "plan_ending")
            else memory
        )
        return PowerSaveDecision("on", reason, ready, updated)

    if not enabled or not planner_enabled:
        return on("powersave_disabled")
    if manual_active:
        return on("manual_override")
    if (
        not readings_fresh
        or soc_pct is None
        or not math.isfinite(soc_pct)
        or not 0 <= soc_pct <= 100
        or battery_power_w is None
        or not math.isfinite(battery_power_w)
    ):
        return on("measurements_unavailable")
    if soc_pct <= settings.min_soc_pct:
        return on("low_soc")
    if battery_power_w != 0:
        return on("battery_active")
    if not plan_valid or schedule_end_s is None or not math.isfinite(schedule_end_s):
        return on("plan_unavailable")
    if next_work_s is not None and (not math.isfinite(next_work_s) or next_work_s > schedule_end_s):
        return on("plan_unavailable")
    if schedule_end_s <= now_s + settings.lead_s:
        return on("plan_ending")
    if now_s >= maintenance + settings.maintenance_interval_s - settings.maintenance_duration_s:
        return on("maintenance_due")
    if next_work_s is not None and next_work_s <= now_s + settings.lead_s:
        return on("planned_work")
    cooldown_start = max(
        memory.started_s,
        last_activity,
        on_since if on_since is not None else memory.started_s,
        memory.last_unavailable_s if memory.last_unavailable_s is not None else memory.started_s,
    )
    if now_s - cooldown_start < settings.cooldown_s:
        return on("cooldown")
    idle_until = min(
        schedule_end_s,
        next_work_s if next_work_s is not None else schedule_end_s,
        maintenance + settings.maintenance_interval_s - settings.maintenance_duration_s,
    )
    if inverter_on is not False and idle_until - now_s < settings.min_off_s + settings.lead_s:
        return on("idle_period_too_short")
    if inverter_on is None:
        return on("inverter_state_unavailable")
    return PowerSaveDecision("off", "idle", False, memory)