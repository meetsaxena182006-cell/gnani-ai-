"""Terminal simulator: talk as a driver or owner without Telegram (text in, everything printed).
Uses the real LLM; set SPEECH_MODE=mock to skip Timbre. Synthetic demo data only.

    python -m saarthi.sim            # then type e.g.  d: diesel 4500 ka daala   |  o: aaj ka kharcha?
    d: = driver of MW-T1, d2: = driver of SG-T1, o: = Mewar owner, loc: 24.51 73.73 [speed], tick
"""
from __future__ import annotations

import asyncio

from . import db, fatigue, seed
from .agents import ceo, driver, mesh
from .bus import Out, bus

CHATS = {"d": ("MW-T1", 11), "d2": ("SG-T1", 21), "d3": ("MW-T2", 12)}


async def printer(o: Out) -> None:
    who = {1001: "OWNER(Mewar)", 2001: "OWNER(Shree)", 11: "DRIVER MW-T1", 21: "DRIVER SG-T1", 12: "DRIVER MW-T2"}.get(o.chat_id, o.chat_id)
    print(f"\n  → [{who}] {o.text}")
    for b in o.buttons:
        print(f"      [button] {b[0]}  ({b[1]})")
    for c in o.contacts:
        print(f"      [contact] {c[0]}: {c[1]}")
    if o.location:
        print(f"      [location] {o.location}")


async def amain() -> None:
    seed.seed(demo=True)
    db.ex("UPDATE fleets SET owner_chat_id=1001 WHERE code='MEWAR'")
    db.ex("UPDATE fleets SET owner_chat_id=2001 WHERE code='SHREE'")
    for code, chat in CHATS.values():
        db.ex("UPDATE vehicles SET driver_chat_id=?, driver_name=? WHERE code=?", (chat, f"Demo driver {code}", code))
    bus.set_sender(printer)
    print(__doc__)
    while True:
        try:
            line = (await asyncio.to_thread(input, "\n> ")).strip()
        except EOFError:
            break
        if not line:
            continue
        if line == "tick":
            await fatigue.tick(); continue
        if line.startswith("accept "):
            await printer(Out(21, await mesh.respond(db.vehicle_by_code("SG-T1"), int(line.split()[1]), True))); continue
        prefix, _, text = line.partition(":")
        text = text.strip()
        if prefix == "o":
            f = db.one("SELECT * FROM fleets WHERE code='MEWAR'")
            await printer(Out(1001, await ceo.handle_owner(f, text)))
        elif prefix == "loc":
            parts = text.split()
            v = db.vehicle_by_code("MW-T1")
            fatigue.update_location(v["id"], float(parts[0]), float(parts[1]), float(parts[2]) if len(parts) > 2 else None)
            print("  location updated")
        elif prefix in CHATS:
            await driver.handle(db.vehicle_by_code(CHATS[prefix][0]), text)
        else:
            print("prefix with d:, d2:, d3:, o:, loc:, or type tick / accept <id>")


if __name__ == "__main__":
    asyncio.run(amain())
