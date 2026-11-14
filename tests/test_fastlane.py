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


async def test_driver_fast_lane_answers_papers_and_weather(fresh, monkeypatch):
    from saarthi import db, geo
    v = db.one("SELECT * FROM vehicles WHERE code='MW-T3'")
    out = await fastlane.driver_fast(dict(v), "mere kagaz theek hain?")
    assert out and ("renew" in out or "valid" in out or "khatam" in out)

    async def fake_weather(lat, lon):
        return {"now": "fog", "temp_c": 14.2, "wind_kmph": 6, "warnings": ["FOG: low visibility"], "source": "test"}
    monkeypatch.setattr(geo, "weather", fake_weather)
    db.ex("UPDATE vehicles SET last_lat=24.6, last_lon=73.7 WHERE id=?", (v["id"],))
    out = await fastlane.driver_fast(dict(db.vehicle(v["id"])), "आगे मौसम कैसा है?")
    assert "कोहरा" in out and "14°C" in out


async def test_driver_fast_lane_leaves_emergencies_and_other_languages_to_the_agent(fresh):
    from saarthi import db
    v = dict(db.one("SELECT * FROM vehicles WHERE code='MW-T1'"))
    assert await fastlane.driver_fast(v, "accident ho gaya, chot lagi hai") is None
    v["lang"] = "te-IN"
    assert await fastlane.driver_fast(v, "mausam kaisa hai") is None


def test_ahead_is_not_fire():
    assert not fastlane._EMERGENCY.search("आगे मौसम कैसा है?")
    assert fastlane._EMERGENCY.search("गाड़ी में आग लग गई")


async def test_owner_papers_fast(fresh):
    from saarthi import db
    f = dict(db.one("SELECT * FROM fleets WHERE code='MEWAR'"))
    out = await fastlane.owner_fast(f, "किस गाड़ी के कागज़ जल्दी ख़त्म हो रहे हैं?")
    assert out and "MW-T" in out
