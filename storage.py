"""Database SQLite: prenotazioni/richieste, ordini, disponibilità, sessioni di conversazione."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from knowledge import TZ

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE / "memphis.db")))
_lock = threading.Lock()

SCHEMA = {
    "requests": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT", "channel": "TEXT", "type": "TEXT", "name": "TEXT",
        "phone": "TEXT", "people": "INTEGER", "day": "TEXT", "time": "TEXT", "room": "TEXT",
        "address": "TEXT", "notes": "TEXT DEFAULT ''", "payload": "TEXT",
        "status": "TEXT DEFAULT 'DA_VERIFICARE'", "customer_confirmed": "INTEGER DEFAULT 0",
        "slot_id": "INTEGER", "notified": "TEXT DEFAULT ''", "created_at": "TEXT", "updated_at": "TEXT",
    },
    "availability": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT", "day": "TEXT NOT NULL", "time": "TEXT NOT NULL",
        "room": "TEXT NOT NULL", "capacity": "INTEGER NOT NULL", "booked": "INTEGER DEFAULT 0",
        "active": "INTEGER DEFAULT 1", "note": "TEXT DEFAULT ''",
    },
    "orders": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT", "channel": "TEXT", "service": "TEXT", "name": "TEXT",
        "phone": "TEXT", "address": "TEXT", "desired_day": "TEXT", "desired_time": "TEXT", "items": "TEXT",
        "notes": "TEXT DEFAULT ''", "total_eur": "REAL", "status": "TEXT DEFAULT 'DA_VERIFICARE'",
        "customer_confirmed": "INTEGER DEFAULT 0", "payment_status": "TEXT DEFAULT 'NON_RICHIESTO'",
        "payment_method": "TEXT", "payment_reference": "TEXT", "paid_at": "TEXT",
        "notified": "TEXT DEFAULT ''", "created_at": "TEXT", "updated_at": "TEXT",
    },
    "sessions": {
        "session_id": "TEXT PRIMARY KEY", "channel": "TEXT", "intent": "TEXT", "draft": "TEXT",
        "history": "TEXT", "updated_at": "TEXT",
    },
}


def stamp():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _lock:
        c = _connect()
        c.execute("PRAGMA journal_mode=WAL")
        for table, cols in SCHEMA.items():
            c.execute(f"CREATE TABLE IF NOT EXISTS {table} (" + ", ".join(f"{k} {v}" for k, v in cols.items()) + ")")
            existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for k, v in cols.items():
                if k not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {k} {v.replace('PRIMARY KEY AUTOINCREMENT', '')}")
        c.commit()
        c.close()


@contextmanager
def db():
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def rows(sql, args=()):
    with db() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    with db() as c:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None


def execute(sql, args=()):
    with db() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def insert(table, data):
    data = {k: v for k, v in data.items() if k in SCHEMA[table]}
    keys = list(data)
    return execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                   [data[k] for k in keys])


def update(table, row_id, data):
    data = {k: v for k, v in data.items() if k in SCHEMA[table] and k != "id"}
    if "updated_at" in SCHEMA[table]:
        data["updated_at"] = stamp()
    if not data:
        return
    execute(f"UPDATE {table} SET {', '.join(k + '=?' for k in data)} WHERE id=?", list(data.values()) + [row_id])


# ---------------------------------------------------------------- sessioni
def load_session(session_id):
    r = one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
    if not r:
        return None
    for k, default in (("draft", {}), ("history", [])):
        try:
            r[k] = json.loads(r[k]) if r[k] else default
        except (TypeError, ValueError):
            r[k] = default
    return r


def save_session(session_id, channel, draft, history):
    execute("""INSERT INTO sessions(session_id,channel,intent,draft,history,updated_at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET channel=excluded.channel, intent=excluded.intent,
               draft=excluded.draft, history=excluded.history, updated_at=excluded.updated_at""",
            (session_id, channel, draft.get("intent") or "", json.dumps(draft, ensure_ascii=False),
             json.dumps(history[-24:], ensure_ascii=False), stamp()))


def delete_session(session_id):
    execute("DELETE FROM sessions WHERE session_id=?", (session_id,))


def decode_order(r):
    if r and isinstance(r.get("items"), str):
        try:
            r["items"] = json.loads(r["items"])
        except ValueError:
            pass
    return r
