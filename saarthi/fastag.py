"""FASTag reconciliation: replace estimated tolls with ACTUAL deductions and catch wrong charges.

Inputs (whatever the owner has):
1. FASTag statement CSV/Excel-exported-CSV from the issuer bank / IHMCL portal -> POST /api/fastag/statement
2. FASTag debit SMS forwarded from the owner's phone (e.g. an SMS-forwarder app -> webhook) -> POST /api/fastag/sms
3. (future) ULIP / issuer API by vehicle number, for approved logistics use cases.

For each deduction: match the GPS toll crossing (same truck, plaza name, within 6 h) -> update the trip's toll to the
actual amount. A deduction with NO matching GPS crossing is flagged to the owner as a possible wrong / double charge
(raise a dispute with the issuer).
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone

from . import db
from .agents import ceo
from .agents.common import inr
from .bus import bus

REG_RE = re.compile(r"\b([A-Z]{2}\s?\d{1,2}\s?[A-Z]{0,4}\s?\d{1,4})\b")
AMT_RE = re.compile(r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
PLAZA_RE = re.compile(r"(?:at|@)\s+(?:toll\s+plaza\s+)?([A-Za-z][A-Za-z .()/&-]{2,60}?)(?:\s+toll|\s+plaza|\s+on\s|\s+for\s|[.,;]|$)", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_vehicle(reg_or_code: str) -> dict | None:
    key = _norm(reg_or_code)
    for v in db.q("SELECT * FROM vehicles"):
        if key and key in (_norm(v["reg_label"]), _norm(v["code"])):
            return v
    return None


def parse_sms(text: str) -> dict | None:
    amt = AMT_RE.search(text)
    if not amt or not re.search(r"fastag|toll", text, re.I):
        return None
    reg = REG_RE.search(text.upper())
    plaza = PLAZA_RE.search(text)
    return {"amount": float(amt.group(1).replace(",", "")), "vehicle": reg.group(1) if reg else None,
            "plaza": plaza.group(1).strip() if plaza else None}


def parse_statement(csv_text: str) -> list[dict]:
    """Flexible CSV: finds amount / plaza (or description) / date / vehicle columns by header name."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    out = []
    for r in rows:
        low = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        pick = lambda *names: next((low[k] for k in low if any(n in k for n in names) and low[k]), "")
        amount = pick("amount", "debit", "toll fee", "txn amt")
        if not amount:
            continue
        try:
            amt = float(re.sub(r"[^\d.]", "", amount))
        except ValueError:
            continue
        if amt <= 0 or re.search(r"recharge|credit|refund", pick("type", "description", "remarks"), re.I):
            continue
        out.append({"amount": amt, "plaza": pick("plaza", "location", "description", "remarks") or None,
                    "vehicle": pick("vehicle", "reg", "vrn") or None, "time": pick("date", "time") or None})
    return out


def _plaza_match(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    first = _norm(re.split(r"[\s(/-]", a.strip())[0])
    return na in nb or nb in na or (len(first) >= 4 and first in nb)


async def reconcile(deduction: dict, default_vehicle: dict | None = None) -> dict:
    v = find_vehicle(deduction.get("vehicle") or "") or default_vehicle
    if not v:
        return {"status": "unknown_vehicle", **deduction}
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    cands = db.q("SELECT * FROM toll_crossings WHERE vehicle_id=? AND crossed_at>=? AND status<>'fastag_actual' ORDER BY id",
                 (v["id"], since))
    match = next((c for c in cands if deduction.get("plaza") and _plaza_match(deduction["plaza"], c["plaza"])), None)
    if match:
        if match["expense_id"]:
            db.ex("UPDATE expenses SET amount=?, note=? WHERE id=?",
                  (deduction["amount"], f"FASTag {match['plaza']} (actual)", match["expense_id"]))
        else:
            eid = db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,note,status,created_at) "
                        "VALUES(?,?, 'toll', ?, ?, 'confirmed', ?)",
                        (v["id"], match["trip_id"], deduction["amount"], f"FASTag {match['plaza']} (actual)", db.now()))
            db.ex("UPDATE toll_crossings SET expense_id=? WHERE id=?", (eid, match["id"]))
        db.ex("UPDATE toll_crossings SET status='fastag_actual' WHERE id=?", (match["id"],))
        db.log_agent("ceo:fastag", f"vehicle:{v['code']}", "fastag_reconciled",
                     {"plaza": match["plaza"], "actual": deduction["amount"], "estimate": match["expected_fee"]})
        return {"status": "matched", "plaza": match["plaza"], "actual": deduction["amount"], "estimate": match["expected_fee"]}
    # deduction without a GPS crossing: possible wrong/double charge
    t = db.active_trip(v["id"])
    db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,note,status,created_at) VALUES(?,?, 'toll', ?, ?, 'confirmed', ?)",
          (v["id"], t["id"] if t else None, deduction["amount"],
           f"FASTag {deduction.get('plaza') or '?'} (no GPS crossing — check)", db.now()))
    db.ex("INSERT INTO watch_events(vehicle_id,kind,detail,created_at) VALUES(?,?,?,?)",
          (v["id"], "fastag_suspect", f"{inr(deduction['amount'])} at {deduction.get('plaza') or '?'}", db.now()))
    f = db.fleet(v["fleet_id"])
    o = ceo.owner_out(f, f"🧾 FASTag check — {v['reg_label']}: {inr(deduction['amount'])} deducted at "
                         f"{deduction.get('plaza') or 'unknown plaza'}, but our GPS shows the truck did not cross it. "
                         f"Possible wrong or double charge: raise a dispute with your FASTag bank.")
    if o:
        await bus.send(o)
    return {"status": "suspect", **deduction}
