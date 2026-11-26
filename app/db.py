"""SQLite connection and schema management."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

DEFAULT_DB_PATH = os.getenv("DB_PATH", "rehearsal.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS deployment_state (
    deployment TEXT PRIMARY KEY,
    version    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_field (
    deployment TEXT NOT NULL,
    field_key  TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (deployment, field_key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS field_owner (
    deployment TEXT NOT NULL,
    field_key  TEXT NOT NULL,
    manager    TEXT NOT NULL,
    position   INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY (deployment, field_key, manager),
    FOREIGN KEY (deployment, field_key)
        REFERENCES config_field (deployment, field_key)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_field_owner_manager
    ON field_owner (deployment, manager);
"""


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection configured for reliable write locking."""
    if db_path != ":memory:":
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None puts the driver in autocommit mode. Transactions are
    # then opened explicitly with BEGIN IMMEDIATE for deterministic locking.
    conn = sqlite3.connect(
        db_path,
        timeout=30.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create the database schema if necessary."""
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def connection_dependency(
    db_path: str = DEFAULT_DB_PATH,
) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency that provides one connection per request."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
