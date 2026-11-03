"""Rule-based safety layer. Escalation decisions are made here in code, not guessed by the LLM."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .config import settings

SEVERITY_ORDER = {"low": 0, "high": 1, "critical": 2}

CRITICAL_PATTERNS = [
    r"accident", r"takkar", r"ghayal", r"injur", r"khoon", r"\bblood", r"\baag\b", r"fire",
    r"palat", r"overturn", r"hospital", r"behosh", r"unconscious", r"loot", r"robbery", r"chori",
    r"maar\s*pee?t", r"attack", r"chot\s+lag", r"dard\s+ho",
]
HIGH_PATTERNS = [
    r"break\s*down", r"breakdown", r"puncture", r"pancture", r"tyre\s*(phat|burst|fat)", r"tair",
    r"phat\s+gaya", r"burst", r"engine\s*(band|bandh|off|garam)", r"band\s+ho\s+gay", r"brake",
    r"overheat", r"garam\s+ho", r"start\s+nahi", r"stuck", r"phas\s+gay", r"clutch", r"radiator",
    r"gear\s*(box)?\s*(toot|kharab)", r"kharab",
]

YES = {"haan", "han", "ha", "haa", "haanji", "ji", "yes", "y", "theek", "thik", "sahi", "ok", "okay",
       "done", "kar", "do", "save", "bilkul", "hmm", "ha ji", "हाँ", "हां", "हा", "जी", "सही", "ठीक"}
NO = {"nahi", "nahin", "na", "no", "n", "galat", "mat", "cancel", "ruko", "नहीं", "नही", "ना", "गलत"}


def rule_severity(text: str) -> str | None:
    t = text.lower()
    if any(re.search(p, t) for p in CRITICAL_PATTERNS):
        return "critical"
    if any(re.search(p, t) for p in HIGH_PATTERNS):
        return "high"
    return None


def final_severity(llm_severity: str | None, text: str) -> str:
    """Take the more serious of the LLM's judgement and the keyword rules."""
    cands = [s for s in (llm_severity, rule_severity(text)) if s in SEVERITY_ORDER]
    return max(cands, key=SEVERITY_ORDER.get) if cands else "low"


def yes_no(text: str) -> bool | None:
    words = re.findall(r"[\wऀ-ॿ]+", text.lower())
    if not words or len(words) > 6:
        return None  # a long sentence is not a bare yes/no; let the agent read it
    has_no = any(w in NO for w in words)
    has_yes = any(w in YES for w in words)
    if has_no and not has_yes:
        return False
    if has_yes and not has_no:
        return True
    return None


def is_moving(v: dict) -> bool:
    if not v.get("last_seen") or v.get("last_speed_kmph") is None:
        return False
    seen = datetime.fromisoformat(v["last_seen"])
    fresh = datetime.now(timezone.utc) - seen < timedelta(minutes=10)
    return fresh and v["last_speed_kmph"] >= settings.moving_speed_kmph


EMERGENCY_NUMBERS = [("Emergency (Police/Ambulance/Fire)", "112"), ("Ambulance", "108"),
                     ("NHAI Highway Helpline", "1033")]
