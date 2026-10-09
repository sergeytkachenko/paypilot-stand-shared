""
import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from app import config, seed

DB_PATH = (Path(os.environ["PAYPILOT_DB"]) if os.environ.get("PAYPILOT_DB")
           else config.DATA_DIR / "paypilot.db")

SCHEMA = """
CREATE TABLE customers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
    tier TEXT NOT NULL, compliance_hold INTEGER NOT NULL DEFAULT 0,
    fx_allowance_used_eur REAL NOT NULL DEFAULT 0);
CREATE TABLE accounts (
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
    currency TEXT NOT NULL, balance REAL NOT NULL);
CREATE TABLE transactions (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
    merchant TEXT NOT NULL, direction TEXT NOT NULL,
    transfer_type TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE disputes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL, account_id TEXT NOT NULL,
    reason_code TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
CREATE TABLE statements_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, sent_to TEXT NOT NULL,
    period TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
"""

STATE_TABLES = ("customers", "accounts", "transactions", "disputes",
                "statements_sent", "escalations")


_path_override: ContextVar[Path | None] = ContextVar("stand_db_path", default=None)


def path() -> Path:
    return _path_override.get() or DB_PATH


@contextmanager
def bound(target: Path):
    token = _path_override.set(target)
    try:
        yield
    finally:
        _path_override.reset(token)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def build(target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".tmp")
    if staging.exists():
        staging.unlink()
    conn = sqlite3.connect(staging)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", seed.CUSTOMERS)
        conn.executemany("INSERT INTO accounts VALUES (?,?,?,?)", seed.ACCOUNTS)
        conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?)",
                         seed.TRANSACTIONS)
        conn.commit()
    finally:
        conn.close()
    os.replace(staging, target)
    return {"seed_version": seed.SEED_VERSION,
            "customers": len(seed.CUSTOMERS),
            "accounts": len(seed.ACCOUNTS),
            "transactions": len(seed.TRANSACTIONS)}


def reset() -> dict:
    ""
    return build(path())


def ensure_seeded() -> None:
    if not path().exists():
        reset()


def rows(query: str, params: tuple = ()) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def one(query: str, params: tuple = ()) -> dict | None:
    result = rows(query, params)
    return result[0] if result else None


def execute(query: str, params: tuple = ()) -> int:
    conn = connect()
    try:
        cur = conn.execute(query, params)
        conn.commit()
        return cur.lastrowid or cur.rowcount
    finally:
        conn.close()


def table_dump(table: str) -> list[dict]:
    if table not in STATE_TABLES:
        raise ValueError(f"Unknown table {table!r}; known: {STATE_TABLES}")
    return rows(f"SELECT * FROM {table}")
