"""SQLite storage: schema and connection helper."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    home_country TEXT NOT NULL,
    home_lat REAL NOT NULL,
    home_lon REAL NOT NULL,
    account_opened TEXT NOT NULL,
    email_changed_at TEXT,
    phone_changed_at TEXT,
    password_changed_at TEXT,
    travel_notice_country TEXT,
    travel_notice_start TEXT,
    travel_notice_end TEXT,
    disputes_90d INTEGER NOT NULL DEFAULT 0,
    disputes_delivered_90d INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cards (
    token TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    last4 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    os TEXT NOT NULL,
    emulator INTEGER NOT NULL DEFAULT 0,
    rooted INTEGER NOT NULL DEFAULT 0,
    proxy_or_vpn INTEGER NOT NULL DEFAULT 0,
    linked_customers TEXT NOT NULL DEFAULT '[]',
    service_fault TEXT
);
CREATE TABLE IF NOT EXISTS merchants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    category_code TEXT NOT NULL,
    country TEXT NOT NULL,
    registered_date TEXT NOT NULL,
    chargeback_rate TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    card_token TEXT NOT NULL REFERENCES cards(token),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    merchant_id TEXT NOT NULL REFERENCES merchants(id),
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    channel TEXT NOT NULL,
    device_id TEXT,
    country TEXT NOT NULL,
    lat REAL,
    lon REAL,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_txn_customer_time ON transactions(customer_id, timestamp);
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    card_token TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    trigger_rule TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    decision TEXT NOT NULL,
    risk_score INTEGER NOT NULL,
    approver TEXT,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS card_actions (
    idempotency_key TEXT PRIMARY KEY,
    card_token TEXT NOT NULL,
    action TEXT NOT NULL,
    alert_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    step TEXT NOT NULL,
    tool TEXT,
    arguments_hash TEXT,
    result_status TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session(path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
