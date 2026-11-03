"""Tests for exporting stored energy in the dearest slots."""

from custom_components.nexus_controller.net_charge import ExportSettings, should_export_peak

# 0.20 earnings, 0.11 export tax, 30 % reserve, 10 kW.
SETTINGS = ExportSettings(0.20, 0.92, 0.11, 0.0, 30.0, 10000.0)


def _export(prices, soc=60.0, stored=0.25):
    return should_export_peak(
        prices_eur_kwh=prices,
        soc_pct=soc,
        stored_value_eur_kwh=stored,
        capacity_kwh=20.8,
        slot_hours=1.0,
        settings=SETTINGS,
    )


def test_exports_only_above_cost_plus_earnings_plus_tax():
    assert not _export([0.55])  # 0.55 - 0.11 = 0.44 < 0.25 + 0.20
    assert _export([0.57])


def test_waits_for_a_higher_price_that_the_energy_can_still_cover():
    # 60 % SOC leaves ~6 kWh to sell, which is one slot at 10 kW: only the top price qualifies.
    assert not _export([0.70, 0.90, 0.60])
    assert _export([0.90, 0.70, 0.60])


def test_more_energy_covers_more_of_the_dear_slots():
    assert _export([0.70, 0.90, 0.60], soc=100.0)


def test_nothing_is_sold_below_the_reserve():
    assert not _export([0.90], soc=30.0)
