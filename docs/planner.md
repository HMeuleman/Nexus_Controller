# Nexus Controller — how the planner works (current implementation)

> Status: descriptive document of the code as it exists on `main` (v0.6.1).
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

## 10. To be implemented — feature specs

### 10.1 Solar source override switch for the planner (internal vs ML) — **IMPLEMENTED** (commit `8dc6c5e`)

Implemented as specced: switch `solar_ml_planner_enabled` (default on), planner input branches in
`_experimental_solar_quarters(now, source)` with `_planner_solar_source()` choosing
`"ml"`/`"internal"`/`"none"`, plan metadata `solar_source`, ML now-sensor independent of the
switch, plan refresh on change. Tests in `tests/test_features.py` (source selection, internal
quarter mapping, ML now-sensor with switch off).

**Original spec:**

**Why:** today the planner takes the ML hourly forecast whenever the ML entities are configured
(`_experimental_solar_quarters`, `coordinator.py:1372`). We want to keep ML as an option but be
able to force the planner onto the *internal* learned forecaster, while **both** predictors keep
being recorded for historical comparison.

**Behavior spec:**

1. New boolean setting `SET_SOLAR_ML_PLANNER` (key e.g. `solar_ml_planner_enabled`), default
   **true** (preserves current behavior: ML is used when configured). Exposed as a normal switch
   entity in `switch.py` (same `_NexusSwitch` pattern as `NexusPlannerSwitch`), with translations
   in `strings.json` / `en.json` / `nl.json`, e.g. NL "Gebruik ML zonne-uurverwachting in planner"
   ("Use ML solar hourly forecast in planner"). Switch ON = ML, OFF = internal.
2. The switch affects **only the planner's solar input** (`_experimental_solar_quarters` and
   therefore `solar_values` / `solar_kwh` in `_async_experimental_quarter_plan`):
   - ON + ML entities configured → current ML path (`hourly_forecast`/`hours` attributes →
     quarter kWh).
   - OFF (or ML not configured) → internal forecaster: map `self.solar_details["slots"]`
     (`pv_w` per quarter start, UTC) to quarter kWh (`pv_w * 0.25 / 1000`), covering today and
     tomorrow like the ML path. If the internal forecaster is disabled (no recorder statistics,
     `_solar_forecast_enabled == False`) or has no slots, solar is omitted from the plan
     (`solar_input = None`, `solar_included: false`) — same as today when ML is missing.
   - Both paths produce the same shape: `dict[UTC quarter start → kWh]`, so nothing downstream
     in `plan_quarters` changes.
3. **Not affected by the switch** (must keep working regardless, so the apexcharts comparison
   stays possible):
   - `solar_forecast_now` sensor (internal, W) — keeps recording the internal current-quarter
     prediction.
   - `solar_ml_forecast_now` sensor (ML, W) — keeps recording the ML current-quarter prediction
     whenever the ML entities are configured, even when the switch is OFF. This sensor is fed in
     `_update_solar_forecast` and must stay independent of the planner source switch.
   - The 24 h internal forecast sensor and its `slots`/`hour_ratio` attributes.
   - `solar_surplus_forecast` (uses the separate `CONF_SOLAR_FORECAST` entity).
4. Observability: add `solar_source: "ml" | "internal" | "none"` to the
   `experimental_quarter_plan` metadata so the plan sensor shows which predictor was used.
5. Changing the switch must trigger a plan refresh: add the new key to the
   `_quarter_refresh_pending` key list in `set_setting()` (`coordinator.py:382`).
6. Edge cases: switch OFF while ML configured → internal used, ML `_nu` sensor still updates;
   switch ON without ML entities → fall back to internal (log once); internal forecaster disabled
   → `solar_source: "none"`, plan proceeds without solar (current behavior).

**Implementation sketch (for the later plan phase):** new const + `SETTING_DEFAULTS` entry in
`const.py`; switch description in `switch.py` + translations; branch in
`_experimental_solar_quarters` (`coordinator.py:1372`) selecting ML vs internal quarters;
`solar_source` in the plan dict (`coordinator.py:1323`); `set_setting` refresh key; leave
`_update_solar_forecast` untouched.

**Tests to add:** planner uses internal slots when switch OFF even with ML entities configured;
uses ML when ON; ML `_nu` sensor still updates when switch OFF; `solar_source` metadata correct;
fallback to internal when ML entities missing; plan refresh triggered on switch change.

### 10.2 New setting sensor: minimum predicted surplus for NOM charge (planner-only) — **IMPLEMENTED** (commit `6dcdbce`)

Implemented as specced: number `nom_charge_min_surplus_w` (W, 0–5000, step 50, default 0),
NL "Minimaal voorspeld zonne-overschot voor NOM-laden (W)"; effective planner threshold
`max(setting, min_control_power_w)`; plan metadata `nom_charge_min_surplus_w`.

**Original spec:**

**Why:** to decide whether a quarter may use NOM charge (charging solar energy into the
battery, `charge_source: "solar"`), the *predicted* solar production minus the *predicted*
complete house usage (house + heatpump) must exceed a threshold. That threshold is
`max(new setting, SET_MIN_CONTROL_POWER)` — i.e. never below the existing
"Minimaal te regelen vermogen (beide richtingen)" (`min_control_power_w`, default 200 W), which
still governs the live PID loop unchanged.

**Sensor definition (this is the part being specced now; planner usage is §10.3, to be discussed):**

1. New number setting `SET_NOM_CHARGE_MIN_SURPLUS_W` (key `nom_charge_min_surplus_w`):
   - unit **W**, range 0–5000, step 50 (same shape as `reserved_room_w`);
   - default / init value **0 W** — the effective threshold is
     `max(0, SET_MIN_CONTROL_POWER)` = `min_control_power_w` (200 W), so a default of 0 simply
     means "no extra margin beyond the minimum control power";
   - entry in `SETTING_DEFAULTS` (`const.py:96`) and a `NexusNumberDescription` in `number.py`
     with `translation_key` = the key, so it appears as a normal config number entity;
   - translations in `strings.json` / `en.json` / `nl.json`, e.g. NL
     "Minimaal voorspeld zonne-overschot voor NOM-laden (W)" (EN "Minimum predicted solar surplus
     for NOM charging (W)");
   - added to the `_quarter_refresh_pending` key list in `set_setting()` (`coordinator.py:382`)
     so changing it re-plans.
2. **Semantics (planner-only):** for each plan slot,
   `predicted_surplus_w(slot) = predicted_solar_w(slot) − predicted_house_w(slot)` where
   - `predicted_solar_w` comes from the planner's selected solar source per §10.1 (ML quarters or
     internal `solar_details` slots, converted to W: quarter kWh × 4000);
   - `predicted_house_w` comes from `self.demand_details["slots"][i]["total_w"]`
     (`demand.py:410` forecast — already the *complete* house usage: baseline + heatpump
     forecast, in W per quarter).
   The slot is eligible for NOM charge only if
   `predicted_surplus_w(slot) > max(SET_NOM_CHARGE_MIN_SURPLUS_W, SET_MIN_CONTROL_POWER)`
   (strictly above).
3. If either prediction is missing for a slot (solar omitted, demand `partial`, ML not
   configured, …) the surplus is unknown → NOM charge is **not** allowed for that slot.
4. The new sensor does **not** change live control: `SET_MIN_CONTROL_POWER` still gates the PID
   in `_async_control` exactly as today; the new setting only feeds the planner.
5. Observability: expose the effective threshold in the `experimental_quarter_plan` metadata,
   e.g. `nom_charge_min_surplus_w = max(new setting, min_control_power_w)`, so the plan sensor
   shows the threshold actually used.

### 10.3 Planner usage of the NOM-charge surplus gate (decided) — **IMPLEMENTED** (commit `6dcdbce`)

Implemented as decided: gate lives in `plan_quarters` (solar surplus energy is 0 for gated
slots, so no `solar_cycle` charge is planned there, including the current quarter);
`current_plan_dispatch` does not re-check it at execution; every plan slot carries
`surplus_forecast_w` (= `predicted_solar_w − predicted_house_w`, `null` when either is
missing). `plan_quarters` takes `house_forecast_w` (per-slot, `None` entry = unknown → gated)
and the effective threshold; the coordinator aligns `demand_details` slots to the price
quarters and adds the slot fields.

**Original spec:**

Decisions (resolving the former open questions):

1. **Enforcement point: `plan_quarters` only, at planning time.** A slot whose predicted surplus
   is not above the threshold can never be part of a `solar_cycle` charge block — the DP simply
   gets no solar-charge option for that slot (equivalently: the solar surplus usable for charging
   in that slot is treated as 0). This includes the currently running quarter, because the gate
   is applied when the plan is generated, before any slot is executed.
2. **No re-validation at execution time.** `current_plan_dispatch`
   (`quarter_planner.py:63`, called from `coordinator.py:2228`) must **not** re-check the
   threshold: it keeps its existing live safety caps (charge ≤ measured surplus, ≤ PV power,
   ≤ limits) but trusts the plan's `charge_source == "solar"` decision as-is. The live
   `SET_MIN_CONTROL_POWER` floor in `_async_control` also stays as-is.
3. **Track the triggering value for debugging/graphing:** every plan slot carries
   `surplus_forecast_w` = `predicted_solar_w − predicted_house_w` for that slot (`null` when
   either prediction is missing). This is the exact value that made the planner choose (or not
   choose) NOM charge for the slot, visible in the `experimenteel_kwartierplan` sensor attributes.

**Implementation sketch:**

- New `plan_quarters` parameters: per-slot house forecast `house_forecast_w: list[float] | None`
  (from `demand_details["slots"][i]["total_w"]`, aligned with the price quarters) and
  `nom_charge_min_surplus_w: float` = the *effective* threshold
  `max(SET_NOM_CHARGE_MIN_SURPLUS_W, SET_MIN_CONTROL_POWER)` computed by the caller.
- Inside `plan_quarters`: per slot compute
  `surplus_w[i] = solar_kwh[i] * 1000 / hours[i] − house_forecast_w[i]` (when both present, else
  `None`); a slot may host solar charging only if `surplus_w[i] is not None and surplus_w[i] >
  nom_charge_min_surplus_w`. Gate the `solar_cycle` branch accordingly (solar surplus energy
  `solar_surplus[index]` treated as 0 for gated slots, so no solar charge power is planned
  there). Grid cycles and discharge are unaffected.
- Coordinator (`_async_experimental_quarter_plan`, `coordinator.py:1204-1365`): build the
  per-slot house forecast list aligned to `forecast` quarters (match `demand_details` slots by
  start time; missing → `None`), pass it plus the effective threshold into `plan_quarters`, and
  add `surplus_forecast_w` to each output slot when building `experimental_quarter_plan["slots"]`.
  Plan metadata already gets `nom_charge_min_surplus_w` (the effective threshold) per §10.2.5.
- `current_plan_dispatch` and `_async_control`: **no changes** for this feature.

**Tests to add:** `plan_quarters` plans no solar charge in slots with surplus ≤ threshold and
solar charge where surplus > threshold; threshold boundary is strict (`>`); missing house or
solar forecast → slot not solar-chargeable; `surplus_forecast_w` present on output slots and
matches solar−house; `current_plan_dispatch` unchanged (still caps at live surplus/PV, no
threshold re-check); effective threshold = max(new setting, min_control_power_w) passed by
coordinator; plan refresh triggered when `nom_charge_min_surplus_w` changes.

### 10.4 New setting sensor: minimum predicted demand for NOM discharge (planner-only) — **IMPLEMENTED** (commit `6dcdbce`)

Implemented as specced: number `nom_discharge_min_demand_w` (W, 0–5000, step 50, default 0), NL
"Minimaal voorspeld huisverbruik voor NOM-ontladen (W)". Below the export reserve the per-slot
`sale_limit` in `plan_quarters` uses `house_forecast_w − solar_w` and is 0 where the predicted
demand is missing or ≤ `max(setting, min_control_power_w)`; the coordinator's `discharge_mode`
annotation uses the per-slot predicted demand + threshold; slots carry `demand_forecast_w`;
plan metadata `nom_discharge_min_demand_w`. No execution-time re-validation.

**Original spec:**

**Why:** mirror of §10.2/§10.3 for the discharge side. NOM discharge delivers power to cover the
house; if the *predicted* house demand for a slot is too small, the delivered power would be
below what the inverter can regulate, so NOM discharge should not be planned there. The planner
input is `max(new setting, SET_MIN_CONTROL_POWER)` — never below the existing
"Minimaal te regelen vermogen (beide richtingen)" (`min_control_power_w`, default 200 W).

**Sensor definition:**

1. New number setting `SET_NOM_DISCHARGE_MIN_DEMAND_W` (key `nom_discharge_min_demand_w`):
   - unit **W**, range 0–5000, step 50;
   - default / init value **0 W** — same reasoning as §10.2: the effective threshold is
     `max(0, SET_MIN_CONTROL_POWER)` = 200 W, i.e. no extra margin by default;
   - entry in `SETTING_DEFAULTS` (`const.py:96`) and a `NexusNumberDescription` in `number.py`
     (`translation_key` = key);
   - translations in `strings.json` / `en.json` / `nl.json`, e.g. NL
     "Minimaal voorspeld huisverbruik voor NOM-ontladen (W)" (EN "Minimum predicted house demand
     for NOM discharge (W)");
   - added to the `_quarter_refresh_pending` key list in `set_setting()` (`coordinator.py:382`).
2. **Semantics (planner-only):** for each plan slot, a slot is eligible for NOM discharge
   (`discharge_mode == "nom"`) only if
   `predicted_house_w(slot) > max(SET_NOM_DISCHARGE_MIN_DEMAND_W, SET_MIN_CONTROL_POWER)`
   (strictly above), where `predicted_house_w(slot)` = `demand_details["slots"][i]["total_w"]`
   (complete house usage incl. heatpump, W per quarter). If the demand prediction is missing for
   a slot → NOM discharge is **not** allowed for that slot (export-mode discharge is unaffected).
3. **Same enforcement rules as §10.3:** enforced in `plan_quarters` at planning time only
   (including the currently running quarter); `current_plan_dispatch` does **not** re-check the
   threshold at execution (it keeps its live caps: NOM discharge ≤ measured demand, ≤ limits);
   the live `SET_MIN_CONTROL_POWER` PID floor is unchanged.
4. **Planner mechanics:**
   - `plan_quarters` reuses the per-slot `house_forecast_w` list from §10.3 and gets
     `nom_discharge_min_demand_w` (effective threshold, computed by the caller). The per-slot
     `sale_limit` (discharge cap below the export reserve) switches from the constant
     `house_load_w` to the per-slot forecast: `sale_limit[i] = max(0, house_forecast_w[i] −
     solar_w[i])`, and is 0 for slots where `house_forecast_w[i] ≤ threshold` (no NOM discharge
     planned there). Discharge above the export reserve (export mode) is not gated.
   - Coordinator slot annotation (`coordinator.py:1366-1381`) uses the per-slot predicted demand:
     `discharge_mode = "nom" if 0 < power_w <= demand_forecast_w and demand_forecast_w >
     threshold else "export" if power_w > 0 else "none"`.
   - Every plan slot additionally carries `demand_forecast_w` (per-slot predicted house demand,
     `null` when missing) next to `surplus_forecast_w` from §10.3, so the value that triggered
     (or blocked) NOM discharge is visible in the plan sensor attributes for debugging/graphing.
   - Plan metadata gets `nom_discharge_min_demand_w` = effective threshold.
5. `current_plan_dispatch` and `_async_control`: **no changes** for this feature.

**Tests to add:** slots with predicted demand ≤ threshold get no NOM discharge (sale_limit 0),
slots above get NOM discharge capped at predicted demand − solar; strict boundary (`>`);
missing demand forecast → no NOM discharge but export unaffected; `demand_forecast_w` on output
slots; `current_plan_dispatch` unchanged; effective threshold = max(new, min_control_power_w);
refresh on setting change.

### 10.5 Expose the five planner actions per slot (`action` field) — **IMPLEMENTED**

**Why:** the planner already distinguishes the five actions per slot, but split across fields
(`power_w` sign, `charge_source`, `discharge_mode`). For visualization (planner overview card)
each slot needs one explicit action label.

**Implemented (commit `70818bf`):** `slot_action(power_w, charge_source, discharge_mode)`
in `quarter_planner.py` maps a slot to `"idle" | "discharge" | "nom_discharge" | "charge" |
"nom_charge"`; the coordinator sets `slot["action"]` on every plan slot (including the active
one) after `discharge_mode` is annotated. So `experimenteel_kwartierplan` attributes
`slots[].action` is directly usable in apexcharts `data_generator` filters, e.g.
`slots.filter(s => s.action === 'nom_discharge').map(s => [new Date(s.start).getTime(), s.power_w])`.

**Tests:** `tests/test_quarter_planner.py::test_slot_action_maps_the_five_planner_actions` and
`test_plan_slots_carry_the_action_field`.

### 10.6 Peak-first greedy planner as a second candidate (hybrid: best of both) — **IMPLEMENTED** (commit `c1b094f`)

**Why:** the DP in `plan_quarters` only expresses *contiguous cycles* — one charge block
immediately feeding one discharge block that returns to the same inventory level. It cannot
express "charge fully at the cheapest hours, then discharge part at the morning peak and the
rest at the evening peak" (one charge feeding two discharge blocks), nor "feed one peak from
two separate cheap charge moments". With volatile prices (e.g. negative night prices) that
restriction costs real money: the observed plan charged only ~7.7 kWh at the night trough
(~35.0 c) because the morning sell block (~3 quarters) capped the cycle's stored energy, and
charged the remaining ~11 kWh at midday (~38.5 c) instead — worth roughly +0.3–0.8 €/day, and
much more when night prices are negative.

**Requested search (user):** find the highest peaks (tracking 3 is enough), for each peak scan
the *whole* earlier period for the cheapest charge moments (respecting room in storage), allow
several charge moments per peak, then move to the next peak.

**Spec:**

1. New pure greedy planner (nested in `plan_quarters` or separate module), same inputs as the DP:
   - **Peak seeds**: local maxima of the export sale price `price_i − tax_i` (a seed must beat
     its left neighbour and not lose to its right neighbour), taken in descending price order with
     pairwise separation ≥ `max(4, count // 10)` slots, up to `MAX_GREEDY_PEAKS = 3` seeds; stop
     early when `sale_price − min(purchase price) < min_profit` (nothing profitable left).
   - **Peak window**: around each seed, expand contiguously while
     `sale_price_j ≥ max(sale_price_seed − min_profit, min_price + min_profit)` and the slot is
     not already used.
   - **Processing order**: the peak loop runs twice — seeds chronologically and seeds by
     descending price — and the attempt with the higher profit wins.
   - **Per peak**: desired discharge = per-slot `min(max_power_w, sale_cap)` over the window,
     where `sale_cap` = `max_power_w` above the export reserve, else the NOM gate
     (`house_w − solar_w`, 0 when unknown or ≤ `nom_discharge_min_demand_w`) — same as the DP's
     `sale_limit`. Available stored energy = SOC-above-`min_soc` at the window start (existing
     inventory counts only when `initial_value_eur_per_kwh` is given, at cost `initial_value`).
   - **Sourcing**: the remaining energy is charged at the cheapest slots *before the window*,
     scanning the whole earlier period: grid charge at slot s costs `price_s` per AC kWh (cap
     `max_power_w × h_s`), solar surplus at slot s costs `price_s − tax_s` (cap
     `solar_surplus_s`, already NOM-gated). Opportunities are sorted by cost; each tentative
     charge recomputes the forward SOC series and is kept only if SOC stays ≤ 100 % (room
     check at that slot). One source kind per slot (`grid` or `solar`), never mixed.
   - **Discharge**: candidates are the window slots sorted by price descending, capped by the
     sellable energy (existing inventory plus the free room above the current SOC, so with no
     `initial_value` existing inventory is not sold); a shrink-to-fit loop drops the
     lowest-priced sale while the margin fails or the SOC bounds would be violated. Charges are
     clamped so the full forward SOC tail stays ≤ 100 %; committed peaks must keep the whole
     series within `[min_soc, 100]`.
   - **Margin filter**: per peak, `gain / sold ≥ min_profit` with
     `gain = Σ sale_price_j × AC_j − (existing_used × initial_value + Σ charge AC × cost)`;
     if it fails, the whole peak (its discharges and its sourced charges) is reverted.
   - **Profit** (for comparison with the DP) uses the same global accounting:
     `Σ sale×sold − Σ purchase×bought − initial_value × max(0, initial_soc − final_soc)`.
2. **Hybrid**: `plan_quarters` computes the DP as today *and* the greedy candidate, and returns
   the one with the higher `profit_eur`. `QuarterPlan` gains a `strategy: str = "dp"` field
   (`"dp"` or `"greedy"`); the coordinator publishes it as `planner_strategy` in the
   `experimental_quarter_plan` metadata and adjusts the `limitations` text accordingly.
3. No changes to `current_plan_dispatch`, `_planner_action`, or the coordinator's slot
   annotation: the greedy output uses the same `power_w` / `charge_source` / `soc_pct` shape.
4. Work stays far below `MAX_PLANNER_WORK` (greedy is O(peaks × count²)).

**Tests to add:** synthetic night-trough + two-peak scenario where one full cheap charge feeds
both peaks (greedy beats DP, `strategy == "greedy"`, SOC within bounds, profit matches a
hand-computed value); one peak fed from two charge blocks because the cheapest block is too
short; margin filter rejects an unprofitable peak; NOM sale cap below the export reserve is
respected; a case where the DP still wins (`strategy == "dp"`); reproduction of the live plan
data showing the greedy profit ≥ DP profit.
