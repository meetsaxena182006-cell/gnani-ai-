import pytest

from saarthi import costing
from saarthi.agents.common import inr


def test_quote_breakdown_matches_hand_calculation():
    r = costing.estimate(costing.CostInputs(distance_km=350, vtype="truck_10w", diesel_price=90,
                                            actual_tolls=1500, target_margin_pct=15))
    # diesel 350/3.5=100 L * 90 = 9000; crew 1 day * 1200; maint 350*6=2100; misc 300; fixed 1 day * 2800
    assert r["diesel_litres"] == 100
    assert r["cost_lines"] == {"diesel": 9000, "tolls": 1500, "driver_and_crew": 1200,
                               "maintenance_tyres": 2100, "misc_loading": 300, "fixed_emi_insurance_permit": 2800}
    assert r["total_cost"] == 16900
    assert r["break_even_freight"] == 16900
    assert r["recommended_quote"] == 19900  # 16900/0.85=19882 -> rounded up to 100


def test_empty_return_and_offer_loss_warning():
    r = costing.estimate(costing.CostInputs(distance_km=700, vtype="truck_10w", diesel_price=90,
                                            empty_return_fraction=1.0, offered_freight=20000))
    assert r["empty_return_km"] == 700
    assert r["trip_days"] == 4
    assert r["profit_at_offer"] < 0
    assert any("BELOW cost" in w for w in r["warnings"])


def test_overload_warning_and_validation():
    r = costing.estimate(costing.CostInputs(distance_km=100, vtype="tempo_lcv", load_tonnes=4))
    assert any("payload" in w for w in r["warnings"])
    with pytest.raises(ValueError):
        costing.estimate(costing.CostInputs(distance_km=0))
    with pytest.raises(ValueError):
        costing.estimate(costing.CostInputs(distance_km=10, vtype="rocket"))


def test_inr_grouping():
    assert inr(4500) == "₹4,500"
    assert inr(125000) == "₹1,25,000"
    assert inr(12345678) == "₹1,23,45,678"
    assert inr(-950) == "-₹950"
