"""Short-lived conversational state shared by agents (in memory)."""
from __future__ import annotations

import time

QUESTION_TTL_S = 180

# vehicle_id -> {"kind": "dial"|"mesh", ...data, "at": epoch}: a yes/no question the companion just asked
questions: dict[int, dict] = {}
# vehicle_id -> {phone: name}; only these (plus emergency numbers) may be dialled
dialable: dict[int, dict[str, str]] = {}


def ask(vid: int, kind: str, **data) -> None:
    questions[vid] = {"kind": kind, "at": time.time(), **data}


def open_question(vid: int) -> dict | None:
    q = questions.get(vid)
    if q and time.time() - q["at"] > QUESTION_TTL_S:
        questions.pop(vid, None)
        return None
    return q
