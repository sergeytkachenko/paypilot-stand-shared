import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, replace
from pathlib import Path

from app import budget, config, db, runctx
from app.agent import loop

TENANTS_DIR = (Path(os.environ["PAYPILOT_TENANTS_DIR"]) if os.environ.get("PAYPILOT_TENANTS_DIR")
               else config.DATA_DIR / "tenants")

SCHEMA = """
CREATE TABLE IF NOT EXISTS key_settings (
    key_id INTEGER PRIMARY KEY REFERENCES keys(id),
    settings TEXT NOT NULL);
"""

_active: ContextVar[budget.Key | None] = ContextVar("stand_workspace_key", default=None)
_header: ContextVar[runctx.RunSettings] = ContextVar("stand_workspace_header",
                                                     default=runctx.RunSettings())
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _connect():
    conn = budget.connect()
    conn.executescript(SCHEMA)
    return conn


def _lock_for(key: budget.Key) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key.id, threading.Lock())


_settings_locks: dict[int, threading.Lock] = {}


def _settings_lock_for(key: budget.Key) -> threading.Lock:
    with _locks_guard:
        return _settings_locks.setdefault(key.id, threading.Lock())


def owner(key: budget.Key) -> str:
    return f"key-{key.id}"


def db_path(key: budget.Key) -> Path:
    return TENANTS_DIR / f"{owner(key)}.db"


def settings(key: budget.Key) -> runctx.RunSettings:
    conn = _connect()
    try:
        row = conn.execute("SELECT settings FROM key_settings WHERE key_id = ?",
                           (key.id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return runctx.RunSettings()
    known = {f.name for f in fields(runctx.RunSettings)}
    stored = {k: v for k, v in json.loads(row["settings"]).items() if k in known}
    return runctx.RunSettings(**stored)


def update(key: budget.Key, **changes) -> runctx.RunSettings:
    with _settings_lock_for(key):
        new = replace(settings(key), **changes)
        conn = _connect()
        try:
            conn.execute("INSERT INTO key_settings (key_id, settings) VALUES (?, ?) "
                         "ON CONFLICT(key_id) DO UPDATE SET settings = excluded.settings",
                         (key.id, json.dumps(new.as_dict())))
            conn.commit()
        finally:
            conn.close()
    return new


def ensure(key: budget.Key) -> None:
    target = db_path(key)
    if target.exists():
        return
    with _lock_for(key):
        if not target.exists():
            db.build(target)


def reset(key: budget.Key) -> dict:
    with _lock_for(key):
        info = db.build(db_path(key))
    loop.reset_sessions(owner(key))
    return info


def drop(key: budget.Key) -> bool:
    target = db_path(key)
    with _lock_for(key):
        existed = target.exists()
        if existed:
            target.unlink()
    conn = _connect()
    try:
        conn.execute("DELETE FROM key_settings WHERE key_id = ?", (key.id,))
        conn.commit()
    finally:
        conn.close()
    loop.reset_sessions(owner(key))
    return existed


def effective(key: budget.Key, header: runctx.RunSettings) -> runctx.RunSettings:
    return runctx.layered(settings(key), header)


def layer_for(header: runctx.RunSettings) -> str:
    return "request" if not header.is_empty() else "key"


def active() -> budget.Key | None:
    return _active.get()


@contextmanager
def bind(key: budget.Key, header: runctx.RunSettings):
    ensure(key)
    key_token = _active.set(key)
    header_token = _header.set(header)
    try:
        with db.bound(db_path(key)), loop.owned_by(owner(key)):
            yield
    finally:
        _header.reset(header_token)
        _active.reset(key_token)


@contextmanager
def write(key: budget.Key, **changes):
    new = update(key, **changes)
    header = _header.get()
    token = runctx.activate(runctx.layered(new, header), layer_for(header))
    try:
        yield
    finally:
        runctx.deactivate(token)
