"""API local para consulta e simulação de entrega de acesso."""

import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field

from .agent import investigate_ticket
from .db import (
    get_connection,
    get_investigation,
    init_db,
    list_investigations as fetch_investigations,
)
from .mcp_client import verify_recovery
from .seed import seed_database

ProducerId = Annotated[str, Header(alias="X-Producer-Id")]
DEFAULT_PRODUCER_ID = "PRODUCER-A"
RECOVERY_ORDER_IDS = ("ORD-101", "ORD-102", "ORD-103")
APP_DIR = Path(__file__).resolve().parent
TEMPLATE_INDEX = APP_DIR / "templates" / "index.html"
STATIC_DIR = APP_DIR / "static"


class InvestigationRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=64)
    ticket_message: str = Field(..., min_length=1, max_length=2000)


class VerifyRecoveryRequest(BaseModel):
    order_ids: list[str] | None = None

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
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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
        elapsed = round(
            (now - datetime.fromisoformat(order["paid_at"])).total_seconds() / 60, 1
        )
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


@app.get("/simulator/orders")
def list_simulator_orders(producer_id: ProducerId = DEFAULT_PRODUCER_ID) -> dict:
    """Lista os pedidos do produtor no ambiente simulado para visualização na interface."""
    with closing(get_connection()) as db:
        rows = db.execute(
            _ORDER_QUERY + "WHERE o.producer_id = ? ORDER BY o.order_id ASC",
            (producer_id,),
        ).fetchall()
        events = _events_by_order(db, producer_id, [row["order_id"] for row in rows])
    now = datetime.now(timezone.utc)
    orders = []
    for row in rows:
        item = _with_delivery_status(row, now)
        order_events = events.get(row["order_id"], [])
        item["last_event"] = order_events[-1] if order_events else None
        orders.append(item)
    return {"producer_id": producer_id, "orders": orders}


@app.post("/investigations")
async def create_investigation(
    payload: InvestigationRequest,
    request: Request,
    producer_id: ProducerId = DEFAULT_PRODUCER_ID,
) -> dict:
    """Executa uma investigação completa via agente Gemini + servidor MCP (RF01–RF04)."""
    api_url = str(request.base_url).rstrip("/")
    try:
        return await investigate_ticket(
            payload.order_id.strip(),
            payload.ticket_message.strip(),
            producer_id=producer_id,
            api_url=api_url,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="Tempo limite excedido ao consultar o modelo de IA. Tente novamente.",
        ) from exc
    except genai_errors.APIError as exc:
        status = 429 if exc.code == 429 else 503
        raise HTTPException(
            status_code=status,
            detail=f"Modelo Gemini indisponível ou limite de cota atingido (HTTP {exc.code}).",
        ) from exc


@app.get("/investigations")
def list_investigations(producer_id: ProducerId = DEFAULT_PRODUCER_ID) -> dict:
    """Retorna o histórico recente de investigações registradas no SQLite."""
    return {"investigations": fetch_investigations(producer_id)}


@app.post("/investigations/{investigation_id}/verify-recovery")
async def verify_investigation_recovery(
    investigation_id: str,
    request: Request,
    payload: VerifyRecoveryRequest | None = None,
    producer_id: ProducerId = DEFAULT_PRODUCER_ID,
) -> dict:
    """Reconsulta os pedidos afetados via MCP sem usar LLM (RF05 / Cenário C)."""
    order_ids = payload.order_ids if payload and payload.order_ids else None
    if order_ids is None:
        investigation = get_investigation(investigation_id, producer_id)
        if investigation is None:
            raise HTTPException(status_code=404, detail="investigation_not_found")
        report = investigation["report"] or {}
        order_ids = report.get("affected_order_ids", [])

    if not order_ids:
        return {
            "checked_order_ids": [],
            "regularized_order_ids": [],
            "pending_order_ids": [],
            "errors": [],
            "summary": "Nenhum pedido afetado para verificar recuperação.",
            "tool_calls": [],
        }

    api_url = str(request.base_url).rstrip("/")
    return await verify_recovery(
        order_ids,
        producer_id=producer_id,
        api_url=api_url,
    )


@app.get("/", response_class=HTMLResponse)
def web_interface() -> HTMLResponse:
    """Serve a interface web principal do YouShop Nexus."""
    return HTMLResponse(TEMPLATE_INDEX.read_text(encoding="utf-8"))
