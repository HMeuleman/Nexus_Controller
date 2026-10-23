from custom_components.nexus_controller.net_charge import NetChargeSettings, should_net_charge

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
