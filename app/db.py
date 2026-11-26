"""Conexão e esquema SQLite do YouShop Nexus."""

import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def _deserialize_investigation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "investigation_id": row["investigation_id"],
        "order_id": row["order_id"],
        "producer_id": row["producer_id"],
        "ticket_message": row["ticket_message"],
        "status": row["status"],
        "report": json.loads(row["result_json"]) if row["result_json"] else None,
        "tool_calls": (
            json.loads(row["tool_calls_json"]) if row["tool_calls_json"] else []
        ),
        "created_at": row["created_at"],
    }


def save_investigation(
    *,
    investigation_id: str,
    order_id: str,
    producer_id: str,
    ticket_message: str,
    status: str,
    result: dict[str, Any],
    tool_calls: list[dict[str, Any]],
    db_path: str | Path | None = None,
) -> None:
    """Persiste uma investigação e o log sanitizado de chamadas MCP (RF01/RF02)."""
    init_db(db_path)
    created_at = datetime.now(timezone.utc).isoformat()
    with closing(get_connection(db_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO investigations (
                investigation_id, order_id, producer_id, ticket_message,
                status, result_json, tool_calls_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                investigation_id,
                order_id,
                producer_id,
                ticket_message,
                status,
                json.dumps(result, ensure_ascii=False),
                json.dumps(tool_calls, ensure_ascii=False),
                created_at,
            ),
        )


def get_investigation(
    investigation_id: str,
    producer_id: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Busca uma investigação específica dentro do escopo do produtor."""
    with closing(get_connection(db_path)) as connection:
        row = connection.execute(
            """
            SELECT investigation_id, order_id, producer_id, ticket_message,
                   status, result_json, tool_calls_json, created_at
            FROM investigations
            WHERE investigation_id = ? AND producer_id = ?
            """,
            (investigation_id, producer_id),
        ).fetchone()
    return _deserialize_investigation(row) if row is not None else None


def list_investigations(
    producer_id: str,
    *,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Lista as investigações mais recentes do produtor."""
    with closing(get_connection(db_path)) as connection:
        rows = connection.execute(
            """
            SELECT investigation_id, order_id, producer_id, ticket_message,
                   status, result_json, tool_calls_json, created_at
            FROM investigations
            WHERE producer_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (producer_id, limit),
        ).fetchall()
    return [_deserialize_investigation(row) for row in rows]
