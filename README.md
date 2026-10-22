# Nexus Controller

An experimental Home Assistant integration for a Zonneplan Nexus battery using the modified Solis S6-EH3P10K-H-ZP inverter.

## Current status

This is an early development preview. Setup lets you select grid power, up to three optional PV production sensors, battery state of charge, electricity price, and candidate charge/discharge entities. The PV diagnostic sensor sums configured sources and normalizes `kW` readings to `W`. With no PV sensors configured it reports `0 W`; if any configured PV source is unavailable or has an unsupported unit, the combined value is unknown. Select each PV source only once, and do not combine an aggregate PV sensor with its component sensors. The integration exposes configured inputs as diagnostic sensors. **It does not write to the inverter or schedule battery operation.** Keep the Zonneplan dongles connected and do not rely on this integration for battery control.

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