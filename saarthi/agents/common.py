from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

CATEGORY_HI = {
    "diesel": "diesel", "toll": "toll", "food": "khana", "repair": "repair", "tyre": "tyre",
    "police_challan": "challan", "loading": "loading-unloading", "parking": "parking",
    "advance_to_driver": "driver advance", "other": "doosra kharcha",
    "freight_advance": "bhaada advance mila", "freight_balance": "bhaada balance mila",
}
EXPENSE_CATEGORIES = ["diesel", "toll", "food", "repair", "tyre", "police_challan", "loading",
                      "parking", "advance_to_driver", "other"]
INCOME_CATEGORIES = ["freight_advance", "freight_balance"]


def inr(x: float | None) -> str:
    """Indian digit grouping: 125000 -> ₹1,25,000."""
    if x is None:
        return "-"
    neg = x < 0
    x = abs(x)
    whole = int(round(x))
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-₹" if neg else "₹") + s


def ist_now() -> datetime:
    return datetime.now(IST)


def today_ist_start_utc() -> str:
    start = ist_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def mask_reg(reg: str) -> str:
    return reg[:4] + "…" + reg[-2:] if len(reg) > 6 else reg
