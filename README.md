# Nexus Controller

![Nexus Controller](custom_components/nexus_controller/brand/icon.png)

An experimental Home Assistant integration for a Zonneplan Nexus battery using the modified Solis S6-EH3P10K-H-ZP inverter.

## Current status

This is an early development preview. Setup lets you select net grid power for each phase, up to three optional PV production sensors, battery state of charge, electricity price, and the four Solis charge/discharge switch and power entities. It also configures a fuse limit per phase (default 25 A) and headroom (default 1,000 W per phase). Phase sensors should use positive values for import and negative values for export. The PV diagnostic sensor sums configured sources and normalizes `kW` readings to `W`. With no PV sensors configured it reports `0 W`; if any configured PV source is unavailable or has an unsupported unit, the combined value is unknown. Select each sensor only once, and do not combine an aggregate PV sensor with its component sensors.

The diagnostic safe charge/discharge power limits assume the inverter changes all three phases evenly. Each phase limit is based on the least available headroom across the three phase measurements, with an additional total Nexus cap of 10 kW. If any phase measurement is unavailable, both limits are unknown. **This preview does not write to the inverter or schedule battery operation.** Keep the Zonneplan dongles connected and do not rely on these diagnostic limits as a live protection system.

After updating from an earlier version, open the integration's **Configure** dialog and select the three phase sensors and four Solis control entities. The former single grid-power entity cannot safely be migrated into three phase measurements.

The four selected Solis controls are currently only mapped, not operated. Before live control is enabled, a charge/discharge mode change will need to disable both modes, zero both power settings, enable only the selected mode, and then apply its requested power. Repeated setpoint writes should be rate-limited; this sequence still needs offline dispatch tests and validation against the inverter before it can control a live battery.

An offline-only dispatch planner is now covered by unit tests. It proposes charging from PV export, charging in an explicitly configured low-price band, or discharging toward zero grid import in an explicitly configured high-price/no-solar band. It observes the phase-derived power caps and explicit SOC boundaries. The planner is not connected to Home Assistant entities or Solis services, and no thresholds are assumed for a live installation.

The control implementation is deliberately deferred until the selected Solis Modbus entity IDs, units, valid ranges, update semantics, and behavior without the Zonneplan dongles have been confirmed. The Tweakers thread provides strategy discussion and partial YAML, not a complete verified control specification.

## Install with HACS

This repository is public but is not currently included in the HACS default catalog. In HACS, open the menu, select **Custom repositories**, add `HMeuleman/Nexus_Controller`, choose **Integration**, and install **Nexus Controller**. Restart Home Assistant and add it under **Settings > Devices & services > Add integration**.

This preview is read-only and is provided for setup and sensor-mapping tests. Installing it does not take over battery operation.

## Issues

Report setup problems or suggestions in the [GitHub issue tracker](https://github.com/HMeuleman/Nexus_Controller/issues).

## Development setup

Use Python 3.12 or newer. From this project directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest
ruff check .
```

For local Home Assistant testing, copy `custom_components/nexus_controller` into the Home Assistant configuration's `custom_components` directory, restart Home Assistant, and add **Nexus Controller** from Devices & Services. This scaffold is not yet suitable for controlling a live battery.

## Planned control work

- Verify input sensor sign conventions, units, freshness, and electricity price attributes.
- Confirm the Solis Modbus writable entities and safe bounds for this inverter/battery combination.
- Define explicit operating modes, minimum reserve, power caps, and a reliable stop/disable behavior.
- Implement and test charge/discharge strategy in simulation before enabling live writes.