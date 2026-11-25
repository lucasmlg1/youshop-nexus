"""API local para consulta e simulação de entrega de acesso."""

import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException

from .db import get_connection, init_db
from .seed import seed_database

ProducerId = Annotated[str, Header(alias="X-Producer-Id")]
DEFAULT_PRODUCER_ID = "PRODUCER-A"
RECOVERY_ORDER_IDS = ("ORD-101", "ORD-102", "ORD-103")

_ORDER_QUERY = """
    SELECT o.*, p.product_name, p.deadline_minutes, a.granted_at
    FROM orders AS o
    JOIN delivery_policies AS p ON p.product_id = o.product_id
    LEFT JOIN access AS a ON a.order_id = o.order_id
"""


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="YouShop Nexus", lifespan=_lifespan)


def _find_order(
    db: sqlite3.Connection, order_id: str, producer_id: str
) -> sqlite3.Row:
    row = db.execute(
        _ORDER_QUERY + "WHERE o.producer_id = ? AND o.order_id = ?",
        (producer_id, order_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    return row


def _with_delivery_status(row: sqlite3.Row, now: datetime) -> dict:
    order = dict(row)
    elapsed = None
    if order["paid_at"] is not None:
        elapsed = (now - datetime.fromisoformat(order["paid_at"])).total_seconds() / 60
    order["elapsed_minutes_since_payment"] = elapsed
    order["past_deadline"] = elapsed is not None and elapsed > order["deadline_minutes"]
    order["has_access"] = order["granted_at"] is not None
    return order


def _events_by_order(
    db: sqlite3.Connection, producer_id: str, order_ids: Sequence[str]
) -> dict[str, list[dict]]:
    if not order_ids:
        return {}
    placeholders = ", ".join("?" for _ in order_ids)
    rows = db.execute(
        f"""
        SELECT e.*
        FROM events AS e
        JOIN orders AS o ON o.order_id = e.order_id
        WHERE o.producer_id = ? AND o.order_id IN ({placeholders})
        ORDER BY e.created_at ASC, e.event_id ASC
        """,
        (producer_id, *order_ids),
    )
    events: dict[str, list[dict]] = {}
    for row in rows:
        event = dict(row)
        if row["details"] is not None:
            event["details"] = json.loads(row["details"])
        events.setdefault(row["order_id"], []).append(event)
    return events


@app.get("/orders/{order_id}")
def get_order(order_id: str, producer_id: ProducerId = DEFAULT_PRODUCER_ID) -> dict:
    with closing(get_connection()) as db:
        order = _find_order(db, order_id, producer_id)
    return _with_delivery_status(order, datetime.now(timezone.utc))


@app.get("/orders/{order_id}/journey")
def get_journey(order_id: str, producer_id: ProducerId = DEFAULT_PRODUCER_ID) -> dict:
    with closing(get_connection()) as db, db:
        db.execute("BEGIN")
        order = _find_order(db, order_id, producer_id)
        events = _events_by_order(db, producer_id, (order_id,))
    return {
        "order": {
            field: order[field]
            for field in (
                "order_id", "producer_id", "product_id", "product_name",
                "integration_id", "payment_status", "created_at", "paid_at",
            )
        },
        "has_access": order["granted_at"] is not None,
        "granted_at": order["granted_at"],
        "events": events.get(order_id, []),
    }


@app.get("/orders/{order_id}/related")
def find_related_orders(
    order_id: str, producer_id: ProducerId = DEFAULT_PRODUCER_ID
) -> dict:
    with closing(get_connection()) as db, db:
        db.execute("BEGIN")
        reference = _find_order(db, order_id, producer_id)
        candidates = db.execute(
            _ORDER_QUERY + """
            WHERE o.producer_id = ? AND o.order_id <> ?
                AND (o.integration_id = ? OR o.product_id = ?)
                AND julianday(o.created_at)
                    BETWEEN julianday(?, '-4 hours') AND julianday(?, '+4 hours')
            ORDER BY o.created_at ASC, o.order_id ASC
            """,
            (
                producer_id, order_id, reference["integration_id"], reference["product_id"],
                reference["created_at"], reference["created_at"],
            ),
        ).fetchall()
        events = _events_by_order(db, producer_id, [row["order_id"] for row in candidates])

    now = datetime.now(timezone.utc)
    return {
        "reference_order_id": order_id,
        "related_orders": [
            {**_with_delivery_status(row, now), "events": events.get(row["order_id"], [])}
            for row in candidates
        ],
    }


@app.get("/products/{product_id}/policy")
def get_delivery_policy(
    product_id: str, producer_id: ProducerId = DEFAULT_PRODUCER_ID
) -> dict:
    with closing(get_connection()) as db:
        policy = db.execute(
            """
            SELECT p.*
            FROM delivery_policies AS p
            WHERE p.product_id = ? AND EXISTS (
                SELECT 1 FROM orders AS o
                WHERE o.product_id = p.product_id AND o.producer_id = ?
            )
            """,
            (product_id, producer_id),
        ).fetchone()
    if policy is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    return dict(policy)


@app.get("/tickets")
def get_related_tickets(
    order_ids: str, producer_id: ProducerId = DEFAULT_PRODUCER_ID
) -> dict:
    ids = [order_id for part in order_ids.split(",") if (order_id := part.strip())]
    if not ids:
        return {"tickets": []}
    placeholders = ", ".join("?" for _ in ids)
    with closing(get_connection()) as db:
        tickets = db.execute(
            f"""
            SELECT t.*
            FROM tickets AS t
            JOIN orders AS o ON o.order_id = t.order_id
            WHERE o.producer_id = ? AND o.order_id IN ({placeholders})
            ORDER BY t.created_at ASC, t.ticket_id ASC
            """,
            (producer_id, *ids),
        ).fetchall()
    return {"tickets": [dict(row) for row in tickets]}


@app.post("/simulator/recover")
def recover(producer_id: ProducerId = DEFAULT_PRODUCER_ID) -> dict:
    granted_at = datetime.now(timezone.utc).isoformat()
    recovered = []
    with closing(get_connection()) as db, db:
        for order_id in RECOVERY_ORDER_IDS:
            inserted = db.execute(
                """
                INSERT INTO access (order_id, granted_at)
                SELECT order_id, ?
                FROM orders
                WHERE producer_id = ? AND order_id = ? AND payment_status = 'paid'
                ON CONFLICT (order_id) DO NOTHING
                """,
                (granted_at, producer_id, order_id),
            )
            if inserted.rowcount:
                db.execute(
                    """
                    INSERT INTO events (order_id, event_type, status, created_at)
                    VALUES (?, 'access_granted', 'ok', ?)
                    """,
                    (order_id, granted_at),
                )
                recovered.append(order_id)
    return {"recovered_order_ids": recovered}


@app.post("/simulator/reset")
def reset() -> dict:
    seed_database()
    return {"status": "ok"}
