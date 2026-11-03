from saarthi.safety import final_severity, rule_severity, yes_no


def test_severity_rules():
    assert rule_severity("bhai accident ho gaya, driver ghayal hai") == "critical"
    assert rule_severity("tyre phat gaya NH48 pe") == "high"
    assert rule_severity("diesel 4500 ka") is None
    # LLM under-rates, rules win
    assert final_severity("low", "gaadi palat gayi") == "critical"
    assert final_severity("critical", "kuch nahi") == "critical"


def test_yes_no():
    assert yes_no("haan") is True
    assert yes_no("Haan ji sahi hai") is True
    assert yes_no("हाँ") is True
    assert yes_no("nahi") is False
    assert yes_no("galat hai") is False
    assert yes_no("diesel 4500 ka daala aur toll 300 diya Udaipur ke paas") is None
