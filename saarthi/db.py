"""SQLite storage. Small fleets = small data; one file, no server."""
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS fleets (
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  owner_chat_id INTEGER, owner_name TEXT, mesh_opt_in INTEGER DEFAULT 1,
  diesel_price REAL DEFAULT 90.0
);
CREATE TABLE IF NOT EXISTS vehicles (
  id INTEGER PRIMARY KEY, fleet_id INTEGER NOT NULL REFERENCES fleets(id),
  code TEXT UNIQUE NOT NULL, reg_label TEXT NOT NULL, vtype TEXT DEFAULT 'truck_10w',
  mileage_loaded REAL DEFAULT 3.5, mileage_empty REAL DEFAULT 4.5,
  driver_name TEXT, driver_chat_id INTEGER, lang TEXT DEFAULT 'hi-IN',
  last_lat REAL, last_lon REAL, last_speed_kmph REAL, last_seen TEXT,
  driving_since TEXT, last_fatigue_ack TEXT, missed_checks INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trips (
  id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
  origin TEXT, destination TEXT, freight_amount REAL, client_name TEXT,
  status TEXT DEFAULT 'active', started_at TEXT, ended_at TEXT, track_token TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL, trip_id INTEGER,
  category TEXT NOT NULL, amount REAL NOT NULL, litres REAL, note TEXT,
  lat REAL, lon REAL, status TEXT DEFAULT 'pending', transcript TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL, kind TEXT, severity TEXT,
  description TEXT, lat REAL, lon REAL, status TEXT DEFAULT 'open', created_at TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL, doc_type TEXT NOT NULL, expiry_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mesh_requests (
  id INTEGER PRIMARY KEY, requester_vehicle INTEGER NOT NULL, helper_vehicle INTEGER,
  need TEXT, lat REAL, lon REAL, status TEXT DEFAULT 'open', created_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_log (
  id INTEGER PRIMARY KEY, ts TEXT, from_agent TEXT, to_agent TEXT, kind TEXT, payload TEXT
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        if settings.db_path != ":memory:":
            os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
    return _conn


def reset(path: str | None = None) -> None:
    """Re-open the DB (used by tests and the seeder)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        if path:
            settings.db_path = path
        conn()


def q(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict[str, Any] | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args: tuple = ()) -> int:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid


# ---------- domain helpers ----------

def vehicle_by_chat(chat_id: int) -> dict | None:
    return one("SELECT * FROM vehicles WHERE driver_chat_id=?", (chat_id,))


def vehicle(vid: int) -> dict | None:
    return one("SELECT * FROM vehicles WHERE id=?", (vid,))


def vehicle_by_code(code: str) -> dict | None:
    return one("SELECT * FROM vehicles WHERE upper(code)=upper(?)", (code,))


def fleet(fid: int) -> dict | None:
    return one("SELECT * FROM fleets WHERE id=?", (fid,))


def fleet_by_owner_chat(chat_id: int) -> dict | None:
    return one("SELECT * FROM fleets WHERE owner_chat_id=?", (chat_id,))


def active_trip(vid: int) -> dict | None:
    return one("SELECT * FROM trips WHERE vehicle_id=? AND status='active' ORDER BY id DESC LIMIT 1", (vid,))


def start_trip(vid: int, origin: str, destination: str, freight: float | None, client: str | None) -> dict:
    ex("UPDATE trips SET status='closed', ended_at=? WHERE vehicle_id=? AND status='active'", (now(), vid))
    tid = ex(
        "INSERT INTO trips(vehicle_id,origin,destination,freight_amount,client_name,started_at,track_token)"
        " VALUES(?,?,?,?,?,?,?)",
        (vid, origin, destination, freight, client, now(), secrets.token_urlsafe(12)),
    )
    return one("SELECT * FROM trips WHERE id=?", (tid,))


def log_agent(from_agent: str, to_agent: str, kind: str, payload: dict) -> None:
    ex(
        "INSERT INTO agent_log(ts,from_agent,to_agent,kind,payload) VALUES(?,?,?,?,?)",
        (now(), from_agent, to_agent, kind, json.dumps(payload, ensure_ascii=False, default=str)),
    )
