"""SQLite persistence: claims, immutable audit trail and API health metrics."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from app import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    chat_id         TEXT,
    employee_name   TEXT,
    purpose         TEXT,
    image_path      TEXT,
    image_hash      TEXT,
    status          TEXT NOT NULL,
    vendor          TEXT,
    receipt_date    TEXT,
    total_amount    REAL,
    currency        TEXT,
    category        TEXT,
    invoice_number  TEXT,
    confidence      REAL,
    extraction_json TEXT,
    risk_score      INTEGER,
    flags_json      TEXT,
    reason          TEXT,
    decided_by      TEXT,
    duplicate_of    TEXT,
    processing_ms   INTEGER,
    ai_latency_ms   INTEGER,
    ai_attempts     INTEGER DEFAULT 0,
    retry_count     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_claims_hash ON claims(image_hash);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    claim_id    TEXT,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    details     TEXT
);

CREATE TABLE IF NOT EXISTS users (
    chat_id     TEXT PRIMARY KEY,
    name        TEXT,
    email       TEXT,
    email_asked INTEGER DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS api_metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    service    TEXT NOT NULL,
    success    INTEGER NOT NULL,
    latency_ms INTEGER,
    error      TEXT,
    model      TEXT
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)


def next_claim_id() -> str:
    year = datetime.now().year
    with _lock, connect() as c:
        row = c.execute("SELECT COUNT(*) FROM claims WHERE id LIKE ?", (f"CLM-{year}-%",)).fetchone()
        return f"CLM-{year}-{row[0] + 1:04d}"


def insert_claim(claim_id: str, **fields) -> None:
    fields.update(id=claim_id, created_at=now(), updated_at=now())
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with connect() as c:
        c.execute(f"INSERT INTO claims ({cols}) VALUES ({marks})", list(fields.values()))


def update_claim(claim_id: str, **fields) -> None:
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE claims SET {sets} WHERE id = ?", [*fields.values(), claim_id])


def get_claim(claim_id: str) -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        return dict(row) if row else None


def find_by_hash(image_hash: str, exclude_id: str) -> Optional[dict]:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM claims WHERE image_hash = ? AND id != ? AND status NOT IN ('FAILED','DUPLICATE') "
            "ORDER BY created_at LIMIT 1", (image_hash, exclude_id)).fetchone()
        return dict(row) if row else None


def candidate_claims(exclude_id: str) -> list:
    """Previously extracted claims, used for fuzzy duplicate detection."""
    with connect() as c:
        rows = c.execute(
            "SELECT id, vendor, receipt_date, total_amount, invoice_number, status FROM claims "
            "WHERE id != ? AND vendor IS NOT NULL AND status NOT IN ('FAILED','DUPLICATE')", (exclude_id,)).fetchall()
        return [dict(r) for r in rows]


def claims_by_status(*statuses: str) -> list:
    marks = ", ".join("?" for _ in statuses)
    with connect() as c:
        rows = c.execute(f"SELECT * FROM claims WHERE status IN ({marks}) ORDER BY created_at", statuses).fetchall()
        return [dict(r) for r in rows]


def claims_for_chat(chat_id: str, limit: int = 10) -> list:
    with connect() as c:
        rows = c.execute("SELECT * FROM claims WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?",
                         (str(chat_id), limit)).fetchall()
        return [dict(r) for r in rows]


def audit(claim_id: Optional[str], actor: str, action: str, from_status: str = None,
          to_status: str = None, details=None) -> None:
    if details is not None and not isinstance(details, str):
        details = json.dumps(details, default=str)
    with connect() as c:
        c.execute("INSERT INTO audit_log (ts, claim_id, actor, action, from_status, to_status, details) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)", (now(), claim_id, actor, action, from_status, to_status, details))


def audit_trail(claim_id: str) -> list:
    with connect() as c:
        rows = c.execute("SELECT * FROM audit_log WHERE claim_id = ? ORDER BY id", (claim_id,)).fetchall()
        return [dict(r) for r in rows]


def get_user(chat_id) -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT * FROM users WHERE chat_id = ?", (str(chat_id),)).fetchone()
        return dict(row) if row else None


_UNSET = object()


def upsert_user(chat_id, name: str, email=_UNSET, email_asked: int = None) -> None:
    """Employee profile: optional notification email, and whether we already asked for it."""
    current = get_user(chat_id) or {}
    new_email = current.get("email") if email is _UNSET else email
    asked = current.get("email_asked", 0) if email_asked is None else email_asked
    with connect() as c:
        c.execute("INSERT INTO users (chat_id, name, email, email_asked, updated_at) VALUES (?, ?, ?, ?, ?) "
                  "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, email=excluded.email, "
                  "email_asked=excluded.email_asked, updated_at=excluded.updated_at",
                  (str(chat_id), name, new_email, asked, now()))


def record_metric(service: str, success: bool, latency_ms: int, error: str = None, model: str = None) -> None:
    with connect() as c:
        c.execute("INSERT INTO api_metrics (ts, service, success, latency_ms, error, model) VALUES (?, ?, ?, ?, ?, ?)",
                  (now(), service, int(success), latency_ms, error, model))


def set_status(claim_id: str, new_status: str, actor: str, action: str, details=None, **fields) -> None:
    """Change status and write the audit entry in one place so every transition is traceable."""
    old = get_claim(claim_id)
    update_claim(claim_id, status=new_status, **fields)
    audit(claim_id, actor, action, old["status"] if old else None, new_status, details)
