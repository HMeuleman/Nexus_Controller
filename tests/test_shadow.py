from datetime import UTC, datetime, timedelta

from custom_components.nexus_controller.dispatch import DispatchSettings
from custom_components.nexus_controller.net_charge import ExportSettings, NetChargeSettings
from custom_components.nexus_controller.shadow import (
    ShadowSettings,
    infer_slot_hours,
    propose_shadow_action,
)

SETTINGS = ShadowSettings(
    dispatch=DispatchSettings(0.15, 0.25, 7.0, 100.0),
    net_charge=NetChargeSettings(0.12, 0.92, 10000.0, 0.08),
    export=ExportSettings(0.12, 0.92, 0.09, 0.08, 30.0, 10000.0),
    capacity_kwh=20.8,
)


def _propose(**overrides):
    values = {
        "phase_imports_w": (700.0, 700.0, 700.0),
        "battery_power_w": 0.0,
        "soc_pct": 80.0,
        "price": 0.30,
        "pv_power_w": 0.0,
        "upcoming_prices": [0.28, 0.27],
        "slot_hours": 0.25,
        "fuse_limit_amps": 25,
        "phase_headroom_w": 1000,
        "settings": SETTINGS,
    }
    values.update(overrides)
    return propose_shadow_action(**values)


def test_discharges_to_cover_import_when_price_is_above_threshold():
    plan = _propose()

    assert (plan.mode, plan.power_w) == ("discharge", 2100.0)


def test_proposal_is_stable_while_the_battery_already_discharges():
    # Grid import is 0 because the battery already delivers 2100 W.
    plan = _propose(phase_imports_w=(0.0, 0.0, 0.0), battery_power_w=2100.0)

    assert (plan.mode, plan.power_w) == ("discharge", 2100.0)


def test_cheap_price_charges_from_the_grid():
    plan = _propose(price=0.10, upcoming_prices=[0.30])

    assert plan.mode == "charge"
    assert plan.reason == "low_price_charge"


def test_price_peak_exports_stored_energy():
    plan = _propose(price=0.80, upcoming_prices=[0.20, 0.22])

    assert (plan.mode, plan.reason) == ("discharge", "export_discharge")


def test_missing_input_gives_an_idle_proposal():
    plan = _propose(battery_power_w=None)

    assert (plan.mode, plan.reason) == ("idle", "input_unavailable")


def test_slot_length_comes_from_forecast_spacing():
    start = datetime(2026, 10, 23, tzinfo=UTC)

    assert infer_slot_hours([start, start + timedelta(minutes=15)]) == 0.25
    assert infer_slot_hours([start, start + timedelta(hours=1)]) == 1.0
    assert infer_slot_hours([]) == 0.25
