# Nexus Controller

![Nexus Controller](custom_components/nexus_controller/brand/icon.png)

A Home Assistant integration that runs a Zonneplan Nexus home battery (modified Solis S6-EH3P10K-H-ZP inverter) with its own energy management strategy: self-consumption (NOM, "nul op de meter") with a PID loop, plus price-based grid charging and selling on dynamic tariffs.

> **This software writes to a battery inverter.** Battery control is **off by default** and always off after a restart. Read the [safety section](#safety-mechanisms) first, start with control off, and keep the Zonneplan dongles connected until you trust it.

## What it does

- Keeps the grid meter near zero when it pays off (NOM), discharging against house load and charging from PV surplus.
- Charges from the grid in cheap slots and sells to the grid in the dearest slots when the price spread covers conversion losses and your desired earnings.
- Tracks what the battery has earned, the value of the energy stored in it, and the number of cycles.
- Works with the Solis Modbus force charge/discharge switches and power numbers (`rc_force_battery_charge`, `rc_force_battery_discharge` and their power entities).
- Optionally powers down the inverter during suitable idle periods, with scheduled wake-up and battery-protection checks.

## Installation

In recent Home Assistant versions HACS appears as the **Marketplace**; the steps are the same. The repository is not in the default catalog, so add it as a custom repository:

1. Open HACS (Marketplace), menu, **Custom repositories**.
2. Add `https://github.com/HMeuleman/Nexus_Controller`, type **Integration**.
3. Install **Nexus Controller** and restart Home Assistant.
4. Add it under **Settings > Devices & services > Add integration**.

## Setup

The setup dialog maps existing entities (each only once):

| Input | Notes |
| --- | --- |
| Grid power L1, L2, L3 | W, positive is import, negative is export |
| Solar power (up to three) | W or kW; `unknown` counts as 0 |
| Battery state of charge | % |
| Electricity price | EUR/kWh including tax |
| Price forecast (optional) | Defaults to the `forecast` attribute of the price sensor. The Zonneplan quarter-hour tariff sensor is recommended: it also carries `price_tax_excluded`, so the exact tax per slot is known |
| Net battery power | W or kW, positive is discharging, negative is charging. Required for control, shadow proposals, the earnings tracker and powersave |
| Solar forecast (optional) | Expected remaining solar energy in kWh until sunset (the coming day at night) |
| Solar ML hourly forecasts (optional) | Select separate sensors for today and tomorrow; used by the planner |
| Solis charge/discharge switches and power numbers | The four control entities |
| Inverter power switch (optional) | Select the actual inverter on/off switch to make powersave available; it is never auto-detected |
| Fuse limit per phase | Default 25 A |

Everything else is a Home Assistant entity (see below), so you can change it without reloading.

## Entities

Settings (configuration entities, values survive restarts):

| Entity | Default | Meaning |
| --- | --- | --- |
| Reserved room per phase | 1000 W | Headroom kept free on each phase |
| Max price for charging from grid | 0.15 | Legacy strategy: charge at or below this price (set to -1 to disable) |
| Minimum price for covering house load (floor) | 0.00 | Lower bound for the NOM discharge threshold |
| Minimum desired earnings per kWh | 0.20 | Wear included |
| Tax per kWh (fallback) | 0.11 | Used when the forecast does not provide the tax |
| Minimum SOC to allow discharge | 10 % | 7 to 99 |
| Control offset | 50 W | Shifts the PID setpoint: slight export when discharging, slight import when charging |
| Minimum power to control | 100 W | Below this (never lower than the offset) the PID does nothing |
| Idle time before reversing direction | 60 s | 0 allowed |
| Solar production above which discharge is blocked | 10000 W | 0 blocks discharge as soon as there is any PV output |
| PID gains and deadbands | kp 0.5, ki 0, kd 0, 15/8 W | kp is defined at 10 s samples |
| Write-down: hours / minimum SOC / percent per day | 24 h, 50 %, 33 % | 0 percent turns it off |
| Manual override timeout | 30 min | See manual override |
| Exported energy paid including tax | off | On for 2026 rules, off from 2027 |
| Inverter powersave cooldown | 15 min | Wait after startup, power-on, battery activity or unavailable inputs |
| Inverter powersave wake lead | 5 min | Wake before planned charging or discharging |
| Inverter maintenance interval / duration | 24 h / 30 min | Periodic continuous power-on period |
| Inverter powersave minimum SOC | 8 % | Keep inverter on at or below this SOC |

Controls: **Battery control enabled**, **Planner**, **Inverter powersave**, **Manual maximum charge** and **Manual maximum discharge**. Battery control starts off after every restart; planner selection starts on. Powersave starts off until explicitly enabled and then restores that choice after a restart.

Tracked values you can overwrite: **Battery earned total**, **Battery cycles total**, **Value of stored energy**.

Diagnostics: tax-free price, tax used, price from which the battery covers house load, battery cycles today, earned today, detected grid sample interval, average daytime house load, expected solar surplus, safe charge/discharge power limits, price forecast statistics, the shadow proposal (mode, power, reason) and the control mode, power and status.

## How the algorithm works

### Prices and tax

Every price slot has a tax: the difference between `price_tax_included` and `price_tax_excluded` when the forecast offers it, otherwise the configured fallback. The price excluding tax is shown as a sensor. Selling to the grid is valued at the price minus tax (from 2027), or at the full price when "exported energy paid including tax" is on (2026).

### The value of stored energy

The integration keeps a running cost basis for the energy in the battery. Charging from the grid adds energy at the import price, charging from PV adds it at the price it could have been exported for, and conversion losses are included. Discharging books the energy at what it saved or sold for, minus its cost basis; that is the "earned" figure. Charge cycles count every percent of SOC gained as 0.01 cycle.

### Decisions

When the planner is off, each time the grid meter reports, the existing strategy decides, in this order:

1. **Sell to the grid.** If the price after export tax beats the stored value plus the desired earnings, and the battery is above its export reserve, it discharges at maximum power, but only in the dearest slots that the available energy can cover. If a dearer slot is coming that the energy can still cover, it waits for it. When export is paid including tax, the export reserve is the configured minimum SOC; otherwise it is the higher of 30 % and the minimum SOC.
2. **Cover house load (NOM).** If the price reaches the discharge threshold, `stored value + desired earnings` (never below the floor setting), the house imports power and SOC is above the minimum, the PID discharges to bring the meter to zero. The threshold follows what the energy actually cost, so cheap stored energy is spent earlier and expensive stored energy is not sold at a loss.
3. **Charge from PV surplus.** When the house exports and the battery is not full, the PID charges to absorb the surplus.
4. **Charge at a low price.** At or below the maximum charging price it charges at the permitted maximum.
5. **Charge from the grid for later.** The cheapest slots in the known forecast are paired with the dearest later slots. A pair is used when the dear price times the round-trip efficiency, minus the cheap price, still earns the desired amount. The planner chooses how many slots and at what power the total profit is highest. Expected solar surplus (from the solar forecast, the average daytime house load and the daylight left) is subtracted from what is bought, and the dear side is valued as the better of use in the house or export.

Charging and discharging both use a fixed assumed efficiency of 97 % (94.09 % round trip), independent of power or whether charging is from grid or PV. This is an estimate, not a measured value.

### Quarter-hour planner

The planner estimates SOC, power and earnings across the available 15-minute prices. It compares multiple chronological charge/discharge cycles, values solar charging at the forgone export price, and applies the desired earnings threshold to each cycle. Export is valued according to the tax switch; NOM uses expected household demand and is allowed down to the minimum SOC. With tax-inclusive export, the minimum SOC is also the export limit. Otherwise export stops at the higher of 30 % and the minimum SOC.

When actual SOC is below the discharge minimum, the plan starts at the measured SOC. A profitable charge/discharge cycle must first charge enough to restore the minimum and retains that energy; planning does not itself force a recovery charge. Live discharge remains blocked at or below the minimum.

Configure the same quarter-hour tariff sensor as both the current electricity price and price forecast, and inspect **Quarter-hour plan** first. Then, only if you choose to control the inverter, turn on **Battery control enabled**. The planner selector alone never enables control. Switching the planner off returns to the existing strategy; manual overrides keep priority.

When the plan or required readings are stale, a price or tax differs from the plan, or the current price does not match the quarter forecast, planner control stops rather than falling back to the existing strategy. `Control: status` reports `planner_price_mismatch` when the selected current price differs. An earnings change requests a new plan immediately; while it is calculating, only the matching committed current quarter can remain active, subject to all live checks. The planner watchdog can stop control even without a new P1 report. An actuator error also clears the planner selection.

Plans are estimates, not guaranteed maximum earnings: the search uses bounded inventory levels and contiguous charge/discharge blocks, a constant daytime average household load and fixed efficiency. Missing solar hours cause the planner to omit the entire solar forecast and report `solar_included: false`. The first calculation starts after settings have been restored; an unavailable plan is retried at most every 30 seconds. Successful plans are recalculated in the background at most once every five minutes, and once when the planner is enabled or desired earnings change. If a calculation is already running, the requested recalculation waits for it to finish. A computation that exceeds its work limit is unavailable. After the first plan, recalculation retains the active quarter's planned action and projects its remaining effect on SOC before optimizing later quarters using current settings. A changed current price or tax makes the plan unavailable until its assumptions match again or the quarter ends. The displayed profit excludes a retained current quarter. Actual SOC, phase caps and other safety checks still apply before each command.

### Inverter powersave

Select the actual inverter power switch and configure the net battery power sensor first. Powersave is off by default and must be explicitly enabled; selecting the planner does not enable it. Once enabled, the setting is restored after restart and the integration requests that the inverter be on at startup. Battery control still starts off. Powersave only switches the inverter off while both planner and battery control are enabled, a fresh valid plan covers the idle period, the SOC and battery power readings are fresh, battery power is exactly zero and both forced-mode switches are confirmed off. A manual override keeps it on.

The inverter stays on for the configured cooldown after startup, a confirmed power-on or any nonzero battery power. Missing or stale readings and unavailable plans restart the cooldown, so inputs must remain healthy for that period before another shutdown. The inverter wakes before planned work and never deliberately starts an off period shorter than 15 minutes; a safety condition or changed schedule can require an earlier wake-up. The inverter is kept on at or below the powersave minimum SOC, and the integration requests a continuous power-on period for the configured maintenance duration once per maintenance interval. It waits at least one minute after confirmed power-on before issuing charge or discharge commands. Missing or stale readings, an unavailable or changed plan, disabled control or planner, and invalid settings request power on. The **Inverter powersave status** sensor reports the decision or a failed write.

If required measurements become unavailable while the inverter is off following an automatic powersave shutdown, the integration requests power on and disables powersave to avoid repeated power cycles. The status reports `measurements_lockout`; investigate the readings before explicitly enabling powersave again. A failed wake request remains a safety issue and is reported as `inverter_write_failed`.

In **Developer Tools > States**, select **Inverter powersave status** to inspect its state and attributes:

| Attribute | Meaning |
| --- | --- |
| `configured` / `enabled` | Whether an inverter switch is selected and powersave is enabled |
| `inverter_power` / `requested_power` | Observed switch state and the requested state; a request is not confirmation of a successful write |
| `measurements_fresh` | Whether the required readings are recent enough for powersave decisions |
| `measurement_max_age_s` | Maximum report age currently accepted for those readings |
| `measurements` | Each configured phase, SOC and battery power entity's state, report age and freshness |
| `measurement_lockout` | Whether powersave was disabled because measurements were lost after an automatic shutdown |
| `inverter_on_since` | When the inverter was observed to turn on |
| `last_unavailable_at` | Most recent observation of unavailable measurements, an unavailable plan or insufficient plan coverage |
| `cooldown_until` | Earliest end of the startup or last-activity cooldown |
| `control_ready_at` | End of the one-minute power-on wait before charge/discharge control |
| `next_planned_work_at` / `planned_wake_at` | Next planned charge/discharge and its calculated early wake time |
| `maintenance_start_at` | Calculated start of the next maintenance power-on period |

Times are UTC estimates and may change with the plan, measurements or settings. An unavailable schedule has no planned-work or wake time. Ending the cooldown does not guarantee shutdown: all safety checks and the minimum idle window must still be satisfied. Timing attributes are not stored in Recorder history.

For example, `cooldown` means the waiting period is still active, `battery_active` means battery power is nonzero, and `low_soc` keeps the inverter on. `plan_unavailable` or `measurements_unavailable` indicates that a safe shutdown cannot be planned; `inverter_write_failed` means a switch command failed.

The powersave implementation is tested offline only. A running Home Assistant, working Modbus connection and correct sensor reports are required; the integration cannot guarantee a wake-up or the maintenance interval during an outage or failed write. Verify the switch and readings and monitor the inverter closely before relying on powersave.

### The PID loop

NOM and PV surplus use an incremental PID on the measured net power: the new power is the measured battery power plus `kp x error` (plus integral and derivative terms if configured). The integration measures how often the meter reports and scales the proportional step for faster meters, so a 1 s meter and a 30 s meter both settle. Extra rules keep it calm:

- Start and stop deadbands prevent hunting around zero; inside the deadband it holds the current power.
- If it overshoots it eases off instead of dropping to zero.
- Below the minimum control power (measured on the house load without the battery) it does not regulate at all.
- The control offset shifts the setpoint, which compensates for a constant measurement bias.
- The proposed power is clamped by the per-phase limits: for each phase the fuse capacity minus the reserved room, using the load without the battery, over three phases and a 10 kW cap.

### Stored value write-down

If the battery stays above the configured SOC and does not discharge for the configured hours (for example after a crash during a planned cycle), the stored value is marked down towards the market: the higher of the running average price and the mean of the dearest forecast slots, by a percentage of the gap per day. It never goes up this way, stops when the battery discharges again, and creates a notification when it starts.

## Safety mechanisms

- **Control is off by default** and after every restart. Turning it off, an error, or unloading the integration releases the battery (both force modes off).
- **Write sequence:** both modes off, power set, then the mode on, rate limited.
- **Keepalive:** the Solis falls back to zero when it is not refreshed, so every 60 s the enable switch is cycled off and on together with the power write. If the inverter drops the mode on its own, it is started again.
- **Idle time between directions:** the battery goes idle for the configured time before it may reverse.
- **Limits:** minimum SOC, 100 % maximum, per-phase headroom and the 10 kW cap also apply to the manual override.
- **Unit check:** the power numbers are read for their unit (W or kW) and limits before writing; other units stop the control.

## Manual override

Set the timeout, then turn on **Manual maximum charge** or **Manual maximum discharge** (they exclude each other). The battery runs at the maximum safe power until the timeout, the SOC limit, or you turn it off. It works with automatic control off, and is always off after a restart.

## Tuning tips

- Start with control off and watch the shadow proposal sensors for a few days.
- If the grid import settles at a constant value while discharging, raise the control offset; check the step attribute of the Solis power number (a step of 100 W hides small corrections).
- For faster settling raise `kp` (try 0.7) and use a small `ki` (0.005).

## Simulation tools

The `sim/` directory has offline tools used to choose the defaults from real Home Assistant history: `build_trace.py` turns a history export into a trace, `closed_loop.py` simulates the strategy against the reconstructed load without the battery, and `replay.py` replays decisions without a plant model. They never talk to Home Assistant.

The diagnostic **Quarter-hour plan** exposes a `slots` attribute with timestamps, prices, estimated power (positive is discharge), charge source and end-of-slot SOC. It is separate from the existing shadow proposal; it controls the battery only when the planner and battery control switches are both on. It requires a contiguous 15-minute price forecast and uses only the remaining duration of the current slot when that slot is available.

In Home Assistant, open the diagnostic sensor's attributes to inspect its plan and estimated profit. A custom forecast-capable dashboard card, such as ApexCharts Card, can plot the `slots` attribute as price, planned power and SOC series. The ordinary history graph cannot show these future values directly.

An [example ApexCharts card](dashboard_planner.txt) is included. Replace its example entity ID with the actual quarter-hour plan sensor ID from your Home Assistant installation.

Optional hourly forecast inputs can be selected explicitly for today's Solar Forecast ML hybrid forecast and tomorrow's Solar Forecast ML forecast. The diagnostic displays each hourly kWh value divided evenly across its four quarters as `solar_forecast_kwh`. When the solar forecast covers every planned quarter, the grid-cycle estimate takes solar surplus and forgone export revenue into account. Otherwise it leaves solar out and reports `solar_included: false`; missing hours are not treated as zero.

Solar-only charging is considered only when paired with a profitable discharge within the forecast. Missing forecast taxes use the configured fallback. The `slots` attribute is not stored in Recorder history.

## Development

Python 3.12 or newer:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest
ruff check .
```

To try it locally, copy `custom_components/nexus_controller` into your Home Assistant `custom_components` directory and restart.

## Issues

Report problems or suggestions in the [GitHub issue tracker](https://github.com/HMeuleman/Nexus_Controller/issues).
