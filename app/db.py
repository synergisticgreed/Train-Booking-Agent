"""SQLite access: schema, connection helper and small query helpers."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    station_code TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    other_names  TEXT,              -- comma-separated aliases, e.g. "Bombay,CST,Mumbai CST"
    city         TEXT
);
CREATE TABLE IF NOT EXISTS trains (
    train_no TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    runs_on  TEXT NOT NULL,         -- e.g. "Mon,Wed,Fri" or "Daily"
    classes  TEXT NOT NULL          -- e.g. "SL,3A,2A"
);
CREATE TABLE IF NOT EXISTS train_stops (
    train_no     TEXT NOT NULL,
    station_code TEXT NOT NULL,
    stop_order   INTEGER NOT NULL,
    arrival      TEXT,              -- HH:MM, NULL at origin
    departure    TEXT,              -- HH:MM, NULL at destination
    day_offset   INTEGER NOT NULL DEFAULT 0,  -- days after the origin departure date
    PRIMARY KEY (train_no, stop_order)
);
CREATE TABLE IF NOT EXISTS fares (
    train_no  TEXT NOT NULL,
    class     TEXT NOT NULL,
    from_code TEXT NOT NULL,
    to_code   TEXT NOT NULL,
    fare      REAL NOT NULL,
    PRIMARY KEY (train_no, class, from_code, to_code)
);
CREATE TABLE IF NOT EXISTS seats (
    train_no     TEXT NOT NULL,
    journey_date TEXT NOT NULL,     -- YYYY-MM-DD, date the train leaves its origin
    class        TEXT NOT NULL,
    available    INTEGER NOT NULL DEFAULT 0,
    rac          INTEGER NOT NULL DEFAULT 0,
    waitlist     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (train_no, journey_date, class)
);
CREATE TABLE IF NOT EXISTS passengers (
    passenger_id       TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    phone_last4        TEXT,
    preferred_language TEXT
);
CREATE TABLE IF NOT EXISTS bookings (
    booking_id   TEXT PRIMARY KEY,
    passenger_id TEXT NOT NULL,
    train_no     TEXT NOT NULL,
    journey_date TEXT NOT NULL,
    from_code    TEXT NOT NULL,
    to_code      TEXT NOT NULL,
    class        TEXT NOT NULL,
    amount_paid  REAL NOT NULL,
    status       TEXT NOT NULL,     -- CONFIRMED | RAC | WAITLISTED | CANCELLED | TRAIN_CANCELLED
    booked_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_travellers (
    booking_id     TEXT NOT NULL,
    traveller_name TEXT NOT NULL,
    age            INTEGER,
    status         TEXT NOT NULL,   -- CNF | RAC | WL
    coach          TEXT,
    berth          TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    action_id    TEXT PRIMARY KEY,
    passenger_id TEXT NOT NULL,
    booking_id   TEXT,
    action_type  TEXT NOT NULL,
    status       TEXT NOT NULL,     -- pending | executing | done | cancelled | expired | failed
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    result       TEXT,              -- JSON, set when done / failed
    payload      TEXT               -- app-added: JSON preview the passenger saw (what will happen)
);
CREATE TABLE IF NOT EXISTS settings (
    name           TEXT NOT NULL,
    value          TEXT NOT NULL,
    source_doc     TEXT,            -- "DOC" or "DOC#section"
    effective_from TEXT NOT NULL DEFAULT '1900-01-01',  -- app-added: lets a circular change a value from a date
    PRIMARY KEY (name, effective_from)
);
-- App-added: the rules / circulars / FAQ corpus, one row per section.
CREATE TABLE IF NOT EXISTS documents (
    doc_id         TEXT NOT NULL,
    section        TEXT NOT NULL,
    doc_type       TEXT NOT NULL,   -- rule | circular | faq
    title          TEXT NOT NULL,
    text           TEXT NOT NULL,
    lang           TEXT NOT NULL DEFAULT 'en',
    topic          TEXT,
    effective_from TEXT NOT NULL,   -- YYYY-MM-DD
    supersedes     TEXT NOT NULL DEFAULT '[]',  -- JSON list of "DOC" or "DOC#section"
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (doc_id, section)
);
-- App-added: audit trail, one row per reply.
CREATE TABLE IF NOT EXISTS traces (
    trace_id     TEXT PRIMARY KEY,
    passenger_id TEXT,
    session_id   TEXT,
    created_at   TEXT NOT NULL,
    payload      TEXT NOT NULL      -- JSON
);
-- App-added: hand-overs to staff desks (R7).
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id   TEXT PRIMARY KEY,
    passenger_id TEXT NOT NULL,
    desk         TEXT NOT NULL,     -- safety | refunds | concessions | complaints | customer_care
    category     TEXT NOT NULL,
    reason       TEXT NOT NULL,     -- passenger_request | low_confidence
    booking_id   TEXT,
    summary      TEXT NOT NULL,
    trace_id     TEXT,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL      -- open
);
CREATE INDEX IF NOT EXISTS idx_bookings_passenger ON bookings(passenger_id);
CREATE INDEX IF NOT EXISTS idx_stops_station ON train_stops(station_code);
"""


def db_path() -> str:
    return os.getenv("DB_PATH", "data/ujjwal.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    Path(db_path()).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction():
    """BEGIN IMMEDIATE: takes the write lock up front, so two confirms can't interleave."""
    Path(db_path()).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------- documents ----------

def upsert_document(doc: dict) -> int:
    """Replace every section of doc["doc_id"] with doc["sections"]. Returns the section count."""
    with connect() as conn:
        conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc["doc_id"],))
        conn.executemany(
            """INSERT INTO documents (doc_id, section, doc_type, title, text, lang, topic,
                                      effective_from, supersedes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    doc["doc_id"], s["section"], doc["doc_type"], doc["title"], s["text"],
                    doc.get("lang", "en"), doc.get("topic"), str(doc["effective_from"]),
                    json.dumps(doc.get("supersedes", [])), _now(),
                )
                for s in doc["sections"]
            ],
        )
    return len(doc["sections"])


def list_documents() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM documents ORDER BY doc_id, section").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["supersedes"] = json.loads(d["supersedes"] or "[]")
        out.append(d)
    return out


# ---------- settings ----------

def upsert_setting(name: str, value, source_doc: str | None, effective_from: str = "1900-01-01") -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO settings (name, value, source_doc, effective_from) VALUES (?, ?, ?, ?)
               ON CONFLICT(name, effective_from) DO UPDATE SET value = excluded.value,
                                                              source_doc = excluded.source_doc""",
            (name, str(value), source_doc, str(effective_from)),
        )


def get_setting(name: str, on_date: str) -> tuple[str, str | None] | None:
    """Value in force on on_date (YYYY-MM-DD): the row with the latest effective_from <= on_date."""
    with connect() as conn:
        row = conn.execute(
            """SELECT value, source_doc FROM settings WHERE name = ? AND effective_from <= ?
               ORDER BY effective_from DESC LIMIT 1""",
            (name, on_date),
        ).fetchone()
    return (row["value"], row["source_doc"]) if row else None


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------- passengers ----------

def passenger_exists(passenger_id: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM passengers WHERE passenger_id = ?", (passenger_id,)
        ).fetchone() is not None


def passenger_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM passengers").fetchone()[0]


# ---------- traces ----------

def save_trace(trace_id: str, passenger_id: str | None, session_id: str | None, payload: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO traces (trace_id, passenger_id, session_id, created_at, payload) VALUES (?, ?, ?, ?, ?)",
            (trace_id, passenger_id, session_id, _now(), json.dumps(payload, ensure_ascii=False, default=str)),
        )


def get_trace(trace_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d
