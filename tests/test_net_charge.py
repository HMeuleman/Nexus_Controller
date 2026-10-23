from custom_components.nexus_controller.net_charge import (
    ExportSettings,
    NetChargeSettings,
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
