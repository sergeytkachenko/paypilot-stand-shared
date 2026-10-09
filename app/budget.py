import hashlib
import math
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config
from app.agent import pricing

HEADER = "X-Stand-Key"
KEY_PREFIX = "psk_"
DAY = timedelta(hours=24)
MONTH = timedelta(days=30)

DB_PATH = (Path(os.environ["PAYPILOT_BUDGET_DB"]) if os.environ.get("PAYPILOT_BUDGET_DB")
           else config.DATA_DIR / "budget.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL, label TEXT NOT NULL,
    daily_usd REAL NOT NULL, monthly_usd REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id INTEGER NOT NULL REFERENCES keys(id), endpoint TEXT NOT NULL,
    model TEXT NOT NULL, input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL,
    priced INTEGER NOT NULL DEFAULT 1, charged_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ledger_key_time ON ledger(key_id, charged_at);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@dataclass(frozen=True)
class Key:
    id: int
    prefix: str
    label: str
    daily_usd: float
    monthly_usd: float
    revoked: bool


def _key(row: sqlite3.Row) -> Key:
    return Key(row["id"], row["prefix"], row["label"], row["daily_usd"],
               row["monthly_usd"], bool(row["revoked"]))


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _limit(name: str, value: float | None) -> float | None:
    if value is not None and not (math.isfinite(value) and value >= 0):
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")
    return value


def create(label: str, daily_usd: float | None = None,
           monthly_usd: float | None = None) -> tuple[Key, str]:
    raw = KEY_PREFIX + secrets.token_urlsafe(24)
    daily = _limit("daily_usd", config.STAND_KEY_DAILY_USD if daily_usd is None else daily_usd)
    monthly = _limit("monthly_usd",
                     config.STAND_KEY_MONTHLY_USD if monthly_usd is None else monthly_usd)
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO keys (key_hash, prefix, label, daily_usd, monthly_usd, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (_hash(raw), raw[:12], label, daily, monthly, _stamp(now())))
        conn.commit()
        row = conn.execute("SELECT * FROM keys WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return _key(row), raw


def resolve(raw: str | None) -> Key | None:
    if not raw:
        return None
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM keys WHERE key_hash = ? AND revoked = 0",
                           (_hash(raw.strip()),)).fetchone()
    finally:
        conn.close()
    return _key(row) if row else None


def find(prefix: str) -> Key:
    conn = connect()
    try:
        found = conn.execute("SELECT * FROM keys WHERE prefix LIKE ?",
                             (prefix + "%",)).fetchall()
    finally:
        conn.close()
    if len(found) != 1:
        raise ValueError(f"{len(found)} keys match {prefix!r}; need exactly one")
    return _key(found[0])


def all_keys() -> list[Key]:
    conn = connect()
    try:
        return [_key(r) for r in conn.execute("SELECT * FROM keys ORDER BY id")]
    finally:
        conn.close()


def revoke(key: Key) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE keys SET revoked = 1 WHERE id = ?", (key.id,))
        conn.commit()
    finally:
        conn.close()


def set_limits(key: Key, daily_usd: float | None, monthly_usd: float | None) -> Key:
    _limit("daily_usd", daily_usd)
    _limit("monthly_usd", monthly_usd)
    conn = connect()
    try:
        conn.execute("UPDATE keys SET daily_usd = COALESCE(?, daily_usd), "
                     "monthly_usd = COALESCE(?, monthly_usd) WHERE id = ?",
                     (daily_usd, monthly_usd, key.id))
        conn.commit()
        return _key(conn.execute("SELECT * FROM keys WHERE id = ?", (key.id,)).fetchone())
    finally:
        conn.close()


def _window(conn: sqlite3.Connection, key_id: int, since: datetime) -> tuple[float, str | None]:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS spent, MIN(charged_at) AS oldest "
        "FROM ledger WHERE key_id = ? AND charged_at > ?",
        (key_id, _stamp(since))).fetchone()
    return round(row["spent"], 6), row["oldest"]


def usage(key: Key) -> dict:
    moment = now()
    conn = connect()
    try:
        day, day_oldest = _window(conn, key.id, moment - DAY)
        month, month_oldest = _window(conn, key.id, moment - MONTH)
    finally:
        conn.close()

    def frees_at(oldest: str | None, span: timedelta) -> str | None:
        return _stamp(datetime.fromisoformat(oldest) + span) if oldest else None

    return {
        "key": key.prefix, "label": key.label, "revoked": key.revoked,
        "day": {"window_hours": 24, "spent_usd": day, "limit_usd": key.daily_usd,
                "remaining_usd": round(max(key.daily_usd - day, 0), 6),
                "oldest_charge_frees_at": frees_at(day_oldest, DAY)},
        "month": {"window_days": 30, "spent_usd": month, "limit_usd": key.monthly_usd,
                  "remaining_usd": round(max(key.monthly_usd - month, 0), 6),
                  "oldest_charge_frees_at": frees_at(month_oldest, MONTH)},
    }


class BudgetExceeded(Exception):
    def __init__(self, window: str, report: dict):
        self.window = window
        self.report = report
        part = report[window]
        super().__init__(
            f"Ліміт ключа {report['key']} вичерпано: "
            f"{'$' + format(part['spent_usd'], '.4f')} із "
            f"{'$' + format(part['limit_usd'], 'g')} за "
            f"{'24 години' if window == 'day' else '30 днів'}. Найстаріша витрата "
            f"виходить із вікна {part['oldest_charge_frees_at']} (UTC).")


class KeyBusy(Exception):
    pass


def check(key: Key) -> dict:
    report = usage(key)
    for window in ("day", "month"):
        if report[window]["spent_usd"] >= report[window]["limit_usd"]:
            raise BudgetExceeded(window, report)
    return report


_active: ContextVar[Key | None] = ContextVar("stand_budget_key", default=None)
_endpoint: ContextVar[str] = ContextVar("stand_budget_endpoint", default="")
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: Key) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key.id, threading.Lock())


@contextmanager
def bind(key: Key, endpoint: str):
    lock = _lock_for(key)
    if not lock.acquire(blocking=False):
        raise KeyBusy(f"Ключ {key.prefix} уже виконує запит; дочекайтеся відповіді "
                      "і повторіть. Одночасно ключ виконує один витратний запит.")
    key_token = _active.set(key)
    endpoint_token = _endpoint.set(endpoint)
    try:
        yield
    finally:
        _endpoint.reset(endpoint_token)
        _active.reset(key_token)
        lock.release()


def active() -> Key | None:
    return _active.get()


def charge(model: str | None, input_tokens: int, output_tokens: int) -> None:
    key = _active.get()
    if key is None or input_tokens + output_tokens == 0:
        return
    cost = pricing.cost_usd(model, input_tokens, output_tokens)
    priced = cost is not None
    if not priced:
        cost = 0.0 if config.LLM_PROVIDER == "mock" else pricing.ceiling_cost_usd(
            input_tokens, output_tokens)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO ledger (key_id, endpoint, model, input_tokens, output_tokens, "
            "cost_usd, priced, charged_at) VALUES (?,?,?,?,?,?,?,?)",
            (key.id, _endpoint.get(), model or "", input_tokens, output_tokens,
             cost, int(priced), _stamp(now())))
        conn.commit()
    finally:
        conn.close()


def record(key: Key, cost_usd: float, at: datetime, endpoint: str = "manual",
           model: str = "manual") -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO ledger (key_id, endpoint, model, input_tokens, output_tokens, "
            "cost_usd, priced, charged_at) VALUES (?,?,?,?,?,?,?,?)",
            (key.id, endpoint, model, 0, 0, cost_usd, 1, _stamp(at)))
        conn.commit()
    finally:
        conn.close()


def effective_models() -> list[tuple[str, str]]:
    if config.LLM_PROVIDER == "mock":
        return []
    if config.LLM_PROVIDER == "anthropic":
        from app.agent.providers.anthropic_provider import DEFAULT_MODEL
    else:
        from app.agent.providers.openai_provider import DEFAULT_MODEL
    out = [("LLM_MODEL", config.LLM_MODEL or DEFAULT_MODEL)]
    if config.EXPLAIN_MODEL:
        out.append(("EXPLAIN_MODEL", config.EXPLAIN_MODEL))
    if config.ROUTER:
        out.append(("TYPESAFE_MODEL", config.TYPESAFE_MODEL))
    return out


def unpriced_models() -> list[str]:
    return [f"{var}={model}" for var, model in effective_models()
            if pricing.price_key(model) is None]


def validate_startup() -> None:
    if not config.STAND_KEYS_REQUIRED:
        return
    missing = unpriced_models()
    if missing:
        raise RuntimeError(
            "STAND_KEYS_REQUIRED=1, але для цих моделей немає ціни в "
            f"app/agent/pricing.py: {', '.join(missing)}. Ліміт ключа не можна "
            "рахувати без ціни — додайте її або оберіть модель із таблиці.")
    connect().close()
