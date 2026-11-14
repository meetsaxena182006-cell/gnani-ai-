from saarthi import fastlane, geo


def test_parses_hindi_route_question():
    q = fastlane.parse_route("MW-T1 को जयपुर से पुणे भेजना है, 12 टन स्टील, भाड़ा 95,000 — रूट बताओ")
    assert q == {"origin": "jaipur", "destination": "pune", "tonnes": 12.0, "freight": 95000.0, "vehicle": "MW-T1"}


def test_parses_hinglish_route_question():
    q = fastlane.parse_route("Udaipur se Hyderabad 15 ton marble, bhada 1,18,000 plan batao")
    assert q["origin"] == "udaipur" and q["destination"] == "hyderabad" and q["freight"] == 118000.0


def test_other_questions_go_to_the_agent():
    assert fastlane.parse_route("कोई गाड़ी खाली है तो उसके लिए लोड ढूंढो") is None
    assert fastlane.parse_route("is mahine ka munafa kitna hai") is None
    assert fastlane.parse_route("Jaipur ka mausam kaisa hai") is None   # one city: not a route


def test_known_city_needs_no_map_lookup():
    assert geo.city_lookup("Pune")["lat"] == 18.5204
    assert geo.city_lookup("उदयपुर")["lon"] == 73.712


def test_price_in_other_forms_and_profit_without_price():
    assert fastlane.parse_route("Udaipur se Ahmedabad 20000 mein load mil raha hai, profit kitna?")["freight"] == 20000.0
    assert fastlane.parse_route("Jaipur se Delhi ₹45,000 me bhejna hai")["freight"] == 45000.0
    assert fastlane.parse_route("Jaipur se Delhi bhejna hai, munafa kitna hoga?") is None
