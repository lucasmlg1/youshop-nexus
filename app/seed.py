"""Popula o SQLite com os cenários de demonstração."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import get_connection, reset_db


def minutes_ago(m: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=m)).isoformat()


def seed_database(db_path: str | Path | None = None) -> None:
    """Recria o banco e carrega os pedidos, eventos e tickets de demonstração."""
    eligibility_rule = (
        "O acesso deve ser liberado em até 15 minutos após a confirmação do "
        "pagamento (payment_status='paid'), salvo em caso de retenção "
        "antifraude ou reembolso."
    )
    policies = [
        ("PROD-01", "Curso Escala Pro", 15, eligibility_rule),
        ("PROD-02", "Comunidade Tráfego Real", 15, eligibility_rule),
        ("PROD-03", "Mentoria Vendas 360", 15, eligibility_rule),
    ]

    # A idade se refere ao pagamento. Para o PIX pendente, à criação do pedido.
    orders = [
        ("ORD-101", "PRODUCER-A", "PROD-01", "INT-YOUCLASS-01", "paid", 110),
        ("ORD-102", "PRODUCER-A", "PROD-01", "INT-YOUCLASS-01", "paid", 95),
        ("ORD-103", "PRODUCER-A", "PROD-01", "INT-YOUCLASS-01", "paid", 70),
        ("ORD-104", "PRODUCER-A", "PROD-01", "INT-YOUCLASS-01", "paid", 45),
        ("ORD-105", "PRODUCER-A", "PROD-01", "INT-YOUCLASS-01", "paid", 6),
        ("ORD-106", "PRODUCER-A", "PROD-01", "INT-YOUCLASS-01", "paid", 120),
        ("ORD-107", "PRODUCER-A", "PROD-01", "INT-YOUCLASS-01", "paid", 180),
        ("ORD-108", "PRODUCER-A", "PROD-02", "INT-MEMBERKIT-02", "paid", 80),
        ("ORD-109", "PRODUCER-A", "PROD-01", "INT-YOUCLASS-01", "pending", 40),
        ("ORD-110", "PRODUCER-A", "PROD-02", "INT-MEMBERKIT-02", "refunded", 150),
        ("ORD-111", "PRODUCER-B", "PROD-03", "INT-HOTCLUB-03", "paid", 90),
        ("ORD-112", "PRODUCER-A", "PROD-02", "INT-MEMBERKIT-02", "paid", 50),
    ]
    order_ages = {}
    order_rows = []
    event_rows = []
    for order_id, producer_id, product_id, integration_id, status, age in orders:
        order_ages[order_id] = age
        paid_at = None
        created_at = minutes_ago(age)
        if status in ("paid", "refunded"):
            paid_at = created_at
            created_at = minutes_ago(age + 5)
            event_rows.append((order_id, "payment_confirmed", None, None, paid_at))
        order_rows.append(
            (order_id, producer_id, product_id, integration_id, status, created_at, paid_at)
        )

    unmapped_orders = ("ORD-101", "ORD-102", "ORD-103", "ORD-104")
    delivered_orders = ("ORD-107", "ORD-108", "ORD-111", "ORD-112")
    unmapped_details = json.dumps(
        {"enrolled": False, "warning": "external_course_id_not_mapped"}
    )
    enrolled_details = json.dumps({"enrolled": True})

    # O último campo indica quantos minutos após o horário de referência ocorreu o evento.
    events = [
        (order_id, "webhook_sent", "http_200", unmapped_details, 1)
        for order_id in unmapped_orders
    ]
    events.extend(
        (order_id, "webhook_sent", "http_200", enrolled_details, 1)
        for order_id in delivered_orders
    )
    events += [
        ("ORD-105", "webhook_sent", "queued", None, 1),
        (
            "ORD-106",
            "antifraud_hold",
            "manual_review",
            json.dumps({"reason": "high_risk_card_review"}),
            1,
        ),
        ("ORD-109", "pix_created", None, None, 0),
        ("ORD-110", "refund_completed", None, None, 5),
    ]
    event_rows.extend(
        (order_id, event_type, status, details, minutes_ago(order_ages[order_id] - offset))
        for order_id, event_type, status, details, offset in events
    )
    access_rows = [
        (order_id, minutes_ago(order_ages[order_id] - 2))
        for order_id in delivered_orders
    ]
    tickets = [
        (
            "TKT-001",
            "ORD-101",
            "Oi, fiz o PIX do Curso Escala Pro faz quase duas horas, já descontou do banco "
            "e chegou o e-mail de compra aprovada, mas não veio nada para entrar nas aulas. "
            "Será que meu pagamento ficou preso ou deu erro no meu e-mail?",
            minutes_ago(0),
        ),
        (
            "TKT-002",
            "ORD-109",
            "Gerei o PIX do curso faz uns 40 minutos e ainda não recebi o acesso.",
            minutes_ago(0),
        ),
        (
            "TKT-003",
            "ORD-112",
            "Comprei a comunidade e não estou conseguindo entrar porque esqueci minha senha.",
            minutes_ago(0),
        ),
    ]

    def populate(connection: sqlite3.Connection) -> None:
        connection.executemany(
            """INSERT INTO delivery_policies (
                product_id, product_name, deadline_minutes, eligibility_rule
            ) VALUES (?, ?, ?, ?)""",
            policies,
        )
        connection.executemany(
            """INSERT INTO orders (
                order_id, producer_id, product_id, integration_id, payment_status, created_at, paid_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            order_rows,
        )
        connection.executemany(
            """INSERT INTO events (order_id, event_type, status, details, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            event_rows,
        )
        connection.executemany(
            "INSERT INTO access (order_id, granted_at) VALUES (?, ?)", access_rows
        )
        connection.executemany(
            "INSERT INTO tickets (ticket_id, order_id, message, created_at) VALUES (?, ?, ?, ?)",
            tickets,
        )

    reset_db(db_path, populate=populate)


if __name__ == "__main__":
    seed_database()
    with closing(get_connection()) as connection:
        for table in (
            "delivery_policies", "orders", "events", "access", "tickets", "investigations"
        ):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table}: {count}")
