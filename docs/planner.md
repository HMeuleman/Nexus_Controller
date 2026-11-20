# Nexus Controller — how the planner works (current implementation)

> Status: descriptive document of the code as it exists on `main` (v0.5.48).
> New features will be described in a later section of this document.

## 1. Big picture: two layers

```
price forecast (entity attr) ─┐
                               ├─► quarter planner (every 5 min) ─► experimental_quarter_plan (sensor attrs)
ML solar hourly (optional) ────┘            │ per-15-min slot: power_w, charge_source, discharge_mode, soc_pct
                                             ▼
                          per control sample (~10 s): _planner_action() validates the current slot
                                             │ against live measurements
                                             ▼
                          _async_control(): watchdog → current_plan_dispatch() → PID (NOM modes) or direct power
                                             ▼
                          actuator → Solis charge/discharge switches + power numbers
```

1. **Quarter planner** (`quarter_planner.py`, pure/offline functions): decides *per 15-minute
   quarter* a battery power for the whole price-forecast horizon. Scheduled by the coordinator
   every `EXPERIMENTAL_PLAN_INTERVAL_S = 300` s (earlier when settings changed), published as the
   `experimental_quarter_plan` dict on sensor `sensor.kelder_nexus_controller_experimenteel_kwartierplan`.
2. **Per-sample controller** (coordinator + `control.py`): every grid sample (~10 s, `sample_estimator`)
   validates the planner's *current* slot against live measurements and turns it into a device
   command — either a fixed power or a closed-loop PID for the NOM modes.

A fallback "shadow" strategy (`shadow.py`) computes a proposal when the planner override is off;
it is also what is published as `shadow_mode`/`shadow_power`/`shadow_reason` sensors.

## 2. The five actions and where they come from

`DispatchPlan(mode, power_w, reason)` (`dispatch.py:24`) — `mode` is one of
`"idle" | "charge" | "discharge"`, `power_w` is a non-negative magnitude.

| Action | `mode` / fields | Typical `reason` | How executed |
|---|---|---|---|
| idle | `idle`, 0 W | `no_dispatch_condition`, `planned_idle_or_limit`, `planner_unavailable_or_stale`, … | nothing written (release) |
| discharge power x | `discharge`, fixed x W | `planned_export`, `export_discharge` | open-loop: actuator set to x |
| charge power x | `charge`, fixed x W | `planned_grid_charge`, `low_price_charge`, `net_charge` | open-loop: actuator set to x |
| NOM discharge (cover house load) | `discharge`, capped at `demand = grid_import_w + battery_power_w` | `high_price_no_solar_reduce_import` | **PID closed loop** (`PID_REASONS`, `coordinator.py:137`) to zero the meter, shifted by `SET_DISCHARGE_OFFSET` |
| NOM charge (absorb PV surplus) | `charge`, capped at `surplus = -(grid_import_w + battery_power_w)` and `pv_power_w` | `pv_surplus_reduce_export` | **PID closed loop** on export → 0 |

The plan slot carries the intent: `power_w` (sign: **+ = discharge, − = charge** in
`plan_quarters` output), `charge_source` (`"grid"` | `"solar"` | `"none"`), `discharge_mode`
(`"nom"` | `"export"` | `"none"`), annotated in `coordinator.py:1366-1381`:
`discharge_mode = "nom" if 0 < power_w <= house_demand else "export" if power_w > 0 else "none"`.

## 3. Quarter plan generation

- Scheduled in `_schedule_experimental_quarter_plan` (`coordinator.py:1134`): at most one task at a
  time, every 300 s, or sooner when `_quarter_refresh_pending` was set by `set_setting()` for
  planner-relevant keys (`min_discharge_soc`, `min_earnings_per_kwh`, `export_includes_tax`,
  `tax`, `reserved_room_w`).
- `_async_experimental_quarter_plan` (`coordinator.py:1204`) builds inputs:
  - **Prices**: `forecast_quarters_with_current(self.price_forecast, now)`
    (`quarter_planner.py:156`) — current partial quarter first with its *remaining* duration
    (`duration_hours < 0.25`), then up to 199 full 15-min quarters; gaps or >200 points → no plan.
  - **Taxes per quarter**: `price_forecast_taxes` (0 if `SET_EXPORT_INCLUDES_TAX`).
  - **Solar per slot**: `_experimental_solar_quarters(now)` (`coordinator.py:1372`) reads the
    configured ML entities (`hourly_forecast` attr for today, `hours` for tomorrow), split into UTC
    quarter kWh by `hourly_forecast_quarters()` (`solar.py:15`). Slot kWh =
    `quarter_kwh * duration / 0.25`. If not all plan slots are covered, `solar_input = None`
    (solar omitted from planning, `solar_included: false`).
  - **House load**: `tracker.state.avg_daytime_load_w` (daytime average).
  - **Economics**: `min_profit = SET_MIN_EARNINGS`; `initial_value = tracker.state.stored_value`
    (value of energy already in the battery); `export_reserve = _export_reserve_soc(min_soc, …)`
    = 30 % unless export includes tax.
- `plan_quarters(...)` (`quarter_planner.py:184`) — dynamic program:
  - Slots are the price quarters; each slot's power is 0 (idle), −charge_w (grid charge) or
    −surplus fraction (solar charge), or +discharge_w.
  - A **cycle** = one contiguous charge block followed by one contiguous discharge block that
    returns to the same inventory level. Grid cycles must earn ≥ `min_profit` per kWh sold
    (sale price net of export tax minus purchase price). Solar cycles charge from PV surplus
    valued at the forgone export price (price − tax) and must also clear the margin.
  - Existing inventory (`stored_value`) may be sold down across a bounded set of levels
    ("initial" moves); a deficit below `min_soc` is replenished first.
  - `sale_limit` caps discharge power so that, below the export reserve, the battery does not
    discharge more than house load minus solar (i.e. planning already embeds NOM behaviour).
  - Efficiency: fixed 97 % per direction (`ONE_WAY_EFFICIENCY`); `use_power_efficiency=False` in
    production. Work is bounded by `MAX_PLANNER_WORK` (20 M) → `ValueError` → plan aborted.
- Output per slot (`coordinator.py:1323-1365`): `start`, `forecast_start`, `duration_hours`,
  `price_eur_kwh`, `export_tax_eur_kwh`, `power_w`, `charge_source`, `soc_pct`, plus
  `solar_forecast_kwh` and `discharge_mode` (except the active slot, which is prepended from the
  previous plan's matching slot = `_quarter_commitment`, so an in-progress quarter keeps its
  committed power).
- Published as `self.experimental_quarter_plan` with metadata (`generated_at`,
  `minimum_earnings_eur_kwh`, `estimated_profit_eur`, `house_load_w`, `solar_included`,
  `limitations`, …) and sensor state `"experimental"` (else `"unavailable"`).

## 4. Validation and execution within the quarter

Every control sample (`_refresh` pipeline, `coordinator.py:537-548`):

1. `_plan(...)` (`coordinator.py:1517`) computes the *shadow* proposal (fallback strategy, see §6).
2. `_async_powersave_step(...)` runs first; it calls `_planner_action` (`coordinator.py:1879`) to
   know whether the plan wants work now and uses `schedule_window(plan slots, now)` for the next
   planned work — the quarter plan also drives inverter powersave (on/off, cooldown, maintenance).
3. `_async_control(data, plan, phases, battery_power, limits)` (`coordinator.py:1613`):
   - control off and no manual mode → idle.
   - `planner_active = SET_PLANNER_OVERRIDE and not manual` (default **on**; `SET_CONTROL_ENABLED`
     gates whether anything is written at all).
   - **Watchdog**: on each new grid sample, if the planner is active and control was on,
     `_planner_action` is recomputed; if it returns non-idle, the *previous* mode/power is kept
     (no flapping within the quarter).
   - If planner active: `plan = watchdog_plan or _planner_action(...)`.
   - `_planner_action` (`coordinator.py:2138`) validates the stored plan against live state:
     plan age ≤ 360 s, `minimum_earnings` unchanged (else the `_quarter_commitment` slot must match
     on `forecast_start/power_w/charge_source/discharge_mode`), slot quotes must still match the
     price forecast, **current price must equal the slot's quoted price** (else
     `planner_price_mismatch` → idle), the current time must fall inside a slot, then
     `current_plan_dispatch(...)` (`quarter_planner.py:63`, called at `coordinator.py:2228`):
     - freshness gate: `measurement_ages_fresh` — phase-group heartbeat plus SOC and battery-power
       reports within `max(120 s, 3 × sample interval)`;
     - `power_w > 0`, `discharge_mode == "nom"`, SOC > min → discharge capped at
       `min(plan power, max_discharge, demand, 10 kW)` (NOM);
     - `power_w > 0`, `discharge_mode == "export"`, SOC > export reserve → discharge
       `min(plan power, max_discharge, 10 kW)`;
     - `power_w < 0`, `charge_source == "solar"` → charge capped at measured surplus and PV;
     - `power_w < 0`, `charge_source == "grid"` → charge `min(-power, max_charge, 10 kW)`;
     - anything else → idle.
   - Execution: if `plan.reason in PID_REASONS` (`"high_price_no_solar_reduce_import"`,
     `"pv_surplus_reduce_export"`) → `_pid_command` (`coordinator.py:1722`) runs
     `simulate_closed_loop_step` (`control.py:117`) on the measured net import, with setpoint
     shifted by `SET_DISCHARGE_OFFSET` (discharge settles at that much export), output capped at
     `min(limits, plan.power_w)`, and a `SET_MIN_CONTROL_POWER` floor below which → idle.
     Deadband handling holds ≥ `HOLD_POWER_W = 50 W` (`deadband_hold`, `overshoot_ease`).
   - All other modes → open-loop: the actuator gets `plan.mode` + `plan.power_w` directly.
   - `actuator.async_apply(mode, power_w, min_interval_s, direction_delay_s=SET_DIRECTION_DELAY)`
     writes the Solis control entities (charge/discharge enable + power). `ActuatorError` disables
     control. Manual mode (`_manual_command`) overrides the plan until its timeout.

## 5. Fallback strategy (planner override off) — the "shadow" plan

`_plan()` → `propose_shadow_action(...)` (`shadow.py:43`):

1. Reconstruct load-without-battery per phase (`phase + battery_power/3`), compute per-phase
   limits from the fuse limit and `SET_RESERVED_ROOM` headroom
   (`calculate_phase_power_limits`).
2. `plan_battery_dispatch(...)` (`dispatch.py:37`): price-band rules —
   discharge if price ≥ discharge threshold (`stored_value + SET_MIN_EARNINGS`, floored by
   `SET_DISCHARGE_PRICE_ABOVE` and charge price + 0.01) and PV ≤ `SET_SOLAR_BLOCK`;
   charge at max if price ≤ `SET_MAX_CHARGE_PRICE`; charge from PV surplus (export) if exporting.
3. Export-peak check (`should_export_discharge`/`should_export_peak`, `export_peak` module) may
   override to a fixed export discharge.
4. Net-charge check (`plan_net_charge`/`should_net_charge`, `net_charge.py:103`): charge from grid
   now if the cheapest future quarters that fill the headroom clear the margin after round-trip
   losses (solar fill subtracted from headroom).

Published as `shadow_*` sensors; used for control when `SET_PLANNER_OVERRIDE` is off.

## 6. Parameters (settings) the planner uses

| Setting (key) | Default (v0.5.47+) | Used for |
|---|---|---|
| `min_earnings_per_kwh` | 0.10 € | min profit per kWh in plan cycles and discharge threshold |
| `tax_per_kwh` / per-quarter taxes | 0.11 € fallback | export tax in sale price |
| `export_includes_tax` | false | tax 0 when export price includes tax |
| `min_discharge_soc` | 10 % | planning floor + NOM discharge gate |
| `max_charge_price` | 0.15 € | grid-charge band (shadow) + threshold floor |
| `discharge_price_above` | 0.0 € | floor for discharge threshold |
| `reserved_room_w` | 500 W | per-phase headroom (shadow limits) |
| `solar_block_discharge_w` | 10000 W | PV above which shadow discharge is inhibited |
| `pid_kp/ki/kd`, `pid_start/stop_error_w` | 0.5/0/0, 15/8 W | closed loop for NOM modes |
| `min_control_power_w` | 200 W | below this the PID is not started |
| `discharge_offset_w` | 50 W | setpoint shift (export when discharging, import when charging) |
| `direction_delay_s` | 75 s | actuator delay before reversing direction |
| `control_enabled`, `planner_override_enabled` | false / true | master switch / planner-vs-shadow |
| `powersave_*` | — | inverter on/off around planned work (uses the plan) |

Constants: `MAX_BATTERY_POWER_W = 10 kW`, `BATTERY_CAPACITY_KWH = 20.8`,
`ONE_WAY_EFFICIENCY = 0.97`, `EXPORT_RESERVE_SOC_PCT = 30`, `MIN_CHARGE_POWER_W = 500`.

## 7. Exposed entities

- `sensor.kelder_nexus_controller_experimenteel_kwartierplan` — state `experimental`/`unavailable`;
  attributes = the full plan dict incl. `slots[]` (per-quarter power/price/source/mode/soc/solar).
- `sensor.kelder_nexus_controller_zonne_opbrengstverwachting_nu` / `..._ml_nu` (W) — internal /
  ML prediction for the current quarter (graphable history).
- `sensor.kelder_nexus_controller_verbruiksverwachting_nu` (W) — demand prediction now.
- `shadow_mode` / `shadow_power` / `shadow_reason`, `control_mode` / `control_power` /
  `control_status` — what the strategy proposes vs what is applied.

## 8. Tests map

- `tests/test_quarter_planner.py` — `plan_quarters` cycles/margins/solar, `current_plan_dispatch`
  (NOM caps, export reserve, solar surplus, freshness), `forecast_quarters*`.
- `tests/test_dispatch.py`, `tests/test_control.py` — dispatch rules and PID simulation.
- `tests/test_shadow.py`, `tests/test_net_charge.py`, `tests/test_export_peak.py` — fallback strategy.
- `tests/test_coordinator.py`, `tests/test_powersave.py`, `tests/test_replay.py`, `sim/closed_loop.py` —
  integration, powersave interplay, offline replay.

## 9. Known limitations (as stated in the plan metadata)

Contiguous cycles only; bounded inventory levels; constant house load; solar omitted unless the
ML forecast covers every plan slot; missing forecast tax falls back to `SET_TAX`; estimated
profit excludes the retained current slot; future phase limits may reduce actual power. The
planner functions are pure (no HA calls); all device writes go through the actuator in
`_async_control` only.
