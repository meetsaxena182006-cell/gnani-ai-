import pytest

from saarthi import db, llm
from saarthi.agents import ceo, driver, mesh
from saarthi.bus import bus
from tests.conftest import FakeLLM

pytestmark = pytest.mark.asyncio


async def test_expense_needs_confirmation_then_saves(fresh):
    llm.set_client(FakeLLM([[("log_entry", {"category": "diesel", "amount": 4500, "litres": 48})], "noted"]))
    v = db.vehicle_by_code("MW-T1")
    reply = await driver.handle(v, "bhaiya 4500 ka diesel daala 48 litre")
    assert "sahi hai" in reply and "₹4,500" in reply and "48 litre" in reply
    assert db.one("SELECT status FROM expenses")["status"] == "pending"
    # driver confirms -> saved, no LLM call needed
    reply = await driver.handle(db.vehicle(v["id"]), "haan")
    assert "Save ho gaya" in reply
    assert db.one("SELECT status FROM expenses")["status"] == "confirmed"
    assert db.q("SELECT kind FROM agent_log WHERE kind='expense_confirmed'")


async def test_expense_rejected_is_not_saved(fresh):
    llm.set_client(FakeLLM([[("log_entry", {"category": "toll", "amount": 300})], "ok"]))
    v = db.vehicle_by_code("MW-T1")
    await driver.handle(v, "toll 300")
    reply = await driver.handle(db.vehicle(v["id"]), "nahi")
    assert "cancel" in reply.lower()
    assert db.one("SELECT status FROM expenses")["status"] == "cancelled"


async def test_big_expense_alerts_owner(fresh):
    llm.set_client(FakeLLM([[("log_entry", {"category": "repair", "amount": 15000, "note": "clutch plate"})], "ok"]))
    v = db.vehicle_by_code("MW-T1")
    await driver.handle(v, "clutch plate 15000 ka laga")
    bus.sent.clear()
    await driver.handle(db.vehicle(v["id"]), "haan")
    assert any(o.chat_id == 1001 and "₹15,000" in o.text for o in bus.sent)


async def test_accident_is_critical_even_if_llm_says_low(fresh):
    llm.set_client(FakeLLM([[("report_breakdown", {"problem": "takkar ho gayi", "severity": "low"})], "ok"]))
    v = db.vehicle_by_code("MW-T1")
    reply = await driver.handle(v, "takkar ho gayi, khalasi ko chot lagi hai")
    assert "112" in reply
    driver_msgs = [o for o in bus.sent if o.chat_id == 11]
    assert any(("Emergency (Police/Ambulance/Fire)", "112") in o.contacts for o in driver_msgs)
    assert any(o.chat_id == 1001 and "EMERGENCY" in o.text for o in bus.sent)
    assert db.one("SELECT severity FROM incidents")["severity"] == "critical"


async def test_breakdown_without_mechanic_triggers_mesh(fresh):
    llm.set_client(FakeLLM([[("report_breakdown", {"problem": "tyre phat gaya", "severity": "high"})],
                            "Malik ko bata diya, paas ke truck se madad maangi hai."]))
    v = db.vehicle_by_code("MW-T1")
    await driver.handle(v, "tyre phat gaya")
    assert any(o.chat_id == 1001 and "Breakdown" in o.text for o in bus.sent)
    # nearby trucks (same fleet first, then other fleet) were asked
    asked = [o for o in bus.sent if any(b[1].startswith("cb:mesh:accept") for b in o.buttons)]
    assert {o.chat_id for o in asked} == {12, 21}
    rid = int(asked[0].buttons[0][1].split(":")[-1])
    helper = db.vehicle_by_code("SG-T1")
    r1 = await mesh.respond(helper, rid, True)
    assert "Shukriya" in r1
    r2 = await mesh.respond(db.vehicle_by_code("MW-T2"), rid, True)
    assert "Kisi aur" in r2
    assert db.one("SELECT status, helper_vehicle FROM mesh_requests")["helper_vehicle"] == helper["id"]
    # both owners informed
    assert any(o.chat_id == 2001 and "helping" in o.text for o in bus.sent)


async def test_ceo_quote_uses_deterministic_engine(fresh):
    llm.set_client(FakeLLM([[("quote_freight", {"origin": "Udaipur", "destination": "Ahmedabad",
                                                 "distance_km": 260, "vtype": "truck_10w", "offered_freight": 20000})],
                            "done"]))
    f = db.one("SELECT * FROM fleets WHERE code='MEWAR'")
    await ceo.handle_owner(f, "Udaipur se Ahmedabad 20000 mein load mil raha hai, profit kitna?")
    fake = llm.get_client()
    tool_msg = [m for m in fake.calls[-1]["messages"] if m["role"] == "tool"][0]["content"]
    assert '"profit_at_offer"' in tool_msg and '"total_cost"' in tool_msg


async def test_daily_digest_lists_expired_docs(fresh):
    f = db.one("SELECT * FROM fleets WHERE code='MEWAR'")
    text = await ceo.daily_digest(f)
    assert "Daily report" in text and "PUC" in text
