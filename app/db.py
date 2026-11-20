"""SQLite connection helpers and schema."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source_files_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendors (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    vendor_id TEXT NOT NULL,
    legal_name TEXT NOT NULL,
    legal_name_norm TEXT NOT NULL,
    status TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    -- Optional trusted remittance data from the vendor master (strings: leading zeros are significant).
    bank_name TEXT,
    routing_number TEXT,
    account_number TEXT,
    verification_phone TEXT,
    PRIMARY KEY (workspace_id, vendor_id)
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    po_id TEXT NOT NULL,
    vendor_id TEXT NOT NULL,
    issued_on TEXT NOT NULL,
    status TEXT NOT NULL,
    currency TEXT NOT NULL,
    description TEXT NOT NULL,
    authorized_net_cents INTEGER NOT NULL CHECK (authorized_net_cents >= 0),
    source_file TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, po_id)
);

CREATE TABLE IF NOT EXISTS po_lines (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    po_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    description TEXT NOT NULL,
    authorized_quantity TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    authorized_net_cents INTEGER NOT NULL CHECK (authorized_net_cents >= 0),
    source_file TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, po_id, line_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    created_at TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    pdf_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('QUEUED','RUNNING','COMPLETED','FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    extraction_json TEXT,
    decision TEXT CHECK (decision IN ('READY','HOLD','BLOCKED')),
    reason_code TEXT,
    reason TEXT,
    decision_json TEXT,
    vendor_id TEXT,
    po_id TEXT,
    invoice_number TEXT,
    invoice_date TEXT,
    gross_cents INTEGER,
    currency TEXT,
    policy_version TEXT,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, created_at);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, id);

CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
    vendor_id TEXT NOT NULL,
    invoice_number_norm TEXT NOT NULL,
    po_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    net_cents INTEGER NOT NULL CHECK (net_cents >= 0),
    tax_cents INTEGER NOT NULL,
    gross_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, vendor_id, invoice_number_norm)
);

CREATE TABLE IF NOT EXISTS allocation_lines (
    allocation_id INTEGER NOT NULL REFERENCES allocations(id),
    po_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    net_cents INTEGER NOT NULL,
    PRIMARY KEY (allocation_id, line_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect() -> sqlite3.Connection:
    # isolation_level=None: we issue BEGIN/COMMIT explicitly.
    conn = sqlite3.connect(config.DB_PATH, timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


VENDOR_REMITTANCE_COLUMNS = ("bank_name", "routing_number", "account_number", "verification_phone")


def init_db() -> None:
    config.ensure_dirs()
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Databases created before the remittance columns existed: add them (all nullable).
        have = {r[1] for r in conn.execute("PRAGMA table_info(vendors)")}
        for col in VENDOR_REMITTANCE_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE vendors ADD COLUMN {col} TEXT")
    finally:
        conn.close()


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def add_event(conn: sqlite3.Connection, run_id: str, stage: str, status: str, message: str,
              data: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO run_events (run_id, ts, stage, status, message, data_json) VALUES (?,?,?,?,?,?)",
        (run_id, now_iso(), stage, status, message, json.dumps(data) if data is not None else None),
    )


def latest_workspace_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT id FROM workspaces ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    return row["id"] if row else None
