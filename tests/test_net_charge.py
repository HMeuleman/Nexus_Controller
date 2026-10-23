import pytest

from custom_components.nexus_controller.net_charge import (
    DischargeThresholdSettings,
    ExportSettings,
    NetChargeSettings,
    discharge_price_threshold,
    should_export_discharge,
    should_net_charge,
)

SETTINGS = NetChargeSettings(0.04, 0.92, 10000.0)


def test_charges_when_a_later_quarter_pays_after_losses_and_margin():
    prices = [0.20, 0.25, 0.45]

    assert should_net_charge(prices_eur_kwh=prices, headroom_kwh=2.5, settings=SETTINGS)


def test_does_not_charge_without_enough_spread():
    prices = [0.30, 0.31, 0.33]

    assert not should_net_charge(prices_eur_kwh=prices, headroom_kwh=2.5, settings=SETTINGS)


def test_does_not_charge_in_dear_quarter_when_cheaper_ones_fill_the_battery():
    prices = [0.30, 0.20, 0.21, 0.50]

    assert not should_net_charge(prices_eur_kwh=prices, headroom_kwh=2.5, settings=SETTINGS)


def test_does_not_charge_when_full_or_without_future():
    assert not should_net_charge(prices_eur_kwh=[0.2, 0.5], headroom_kwh=0, settings=SETTINGS)
    assert not should_net_charge(prices_eur_kwh=[0.2], headroom_kwh=2.5, settings=SETTINGS)


def test_export_requires_margin_after_tax_wear_and_refill_cost():
    settings = ExportSettings(0.04, 1.0, 0.09, 0.05, 30.0, 10000.0)
    prices = [0.50, 0.20]

    assert should_export_discharge(prices_eur_kwh=prices, soc_pct=80, settings=settings)
    assert not should_export_discharge(prices_eur_kwh=[0.35, 0.20], soc_pct=80, settings=settings)
    assert not should_export_discharge(prices_eur_kwh=prices, soc_pct=30, settings=settings)


def test_discharge_threshold_reserves_energy_for_the_dearest_quarters():
    settings = DischargeThresholdSettings(1000.0, 1.0, 0.0, 0.0)
    prices = [0.30, 0.80, 0.40, 0.35]
    # 0.5 kWh at 1 kW covers two quarters -> reserve for the two dearest: 0.80 and 0.40
    assert (
        discharge_price_threshold(prices_eur_kwh=prices, stored_kwh=0.5, settings=settings) == 0.40
    )


def test_discharge_threshold_never_undercuts_refill_cost_plus_margin():
    settings = DischargeThresholdSettings(1000.0, 0.9, 0.08, 0.12)
    prices = [0.30, 0.31, 0.32]
    result = discharge_price_threshold(prices_eur_kwh=prices, stored_kwh=5.0, settings=settings)
    assert result == pytest.approx(0.30 / 0.9 + 0.20)


def test_discharge_threshold_is_unknown_without_energy_or_prices():
    settings = DischargeThresholdSettings(1000.0, 1.0, 0.0, 0.0)
    assert discharge_price_threshold(prices_eur_kwh=[], stored_kwh=1.0, settings=settings) is None
    assert (
        discharge_price_threshold(prices_eur_kwh=[0.4], stored_kwh=0.0, settings=settings) is None
    )


def test_slot_length_scales_how_many_slots_a_battery_needs():
    hourly = NetChargeSettings(0.04, 1.0, 10000.0, 0.0, 1.0)
    prices = [0.20, 0.21, 0.22, 0.80, 0.80]

    # 10 kWh at 10 kW needs one hourly slot, so only the cheapest slot qualifies.
    assert should_net_charge(prices_eur_kwh=prices, headroom_kwh=10.0, settings=hourly)
    assert not should_net_charge(
        prices_eur_kwh=[0.21, 0.20, 0.22, 0.80], headroom_kwh=10.0, settings=hourly
    )
