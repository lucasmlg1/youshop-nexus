"""Conexão e esquema SQLite do YouShop Nexus."""

import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "youshop.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_policies (
    product_id TEXT PRIMARY KEY NOT NULL,
    product_name TEXT NOT NULL,
    deadline_minutes INTEGER NOT NULL,
    eligibility_rule TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY NOT NULL,
    producer_id TEXT NOT NULL,
    product_id TEXT NOT NULL REFERENCES delivery_policies (product_id),
    integration_id TEXT NOT NULL,
    payment_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    paid_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders (order_id),
    event_type TEXT NOT NULL,
    status TEXT,
    details TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access (
    order_id TEXT PRIMARY KEY NOT NULL REFERENCES orders (order_id),
    granted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders (order_id),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investigations (
    investigation_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL,
    producer_id TEXT NOT NULL,
    ticket_message TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    tool_calls_json TEXT,
    created_at TEXT NOT NULL
);
"""

_DROP_TABLES = """
DROP TABLE IF EXISTS investigations;
DROP TABLE IF EXISTS tickets;
DROP TABLE IF EXISTS access;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS delivery_policies;
"""


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Abre uma conexão com chaves estrangeiras ativas."""
    if db_path is None:
        db_path = Path(os.getenv("NEXUS_DB_PATH", DEFAULT_DB_PATH))
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: str | Path | None = None) -> None:
    """Cria as tabelas ausentes e preserva os dados existentes."""
    with closing(get_connection(db_path)) as connection, connection:
        connection.executescript("BEGIN;\n" + _SCHEMA)


def reset_db(
    db_path: str | Path | None = None,
    *,
    populate: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    """Recria as tabelas e carrega os dados iniciais na mesma transação."""
    with closing(get_connection(db_path)) as connection, connection:
        connection.executescript("BEGIN;\n" + _DROP_TABLES + _SCHEMA)
        if populate is not None:
            populate(connection)
