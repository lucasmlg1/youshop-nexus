import os
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

API_BASE_URL = os.getenv("NEXUS_API_URL", "http://127.0.0.1:8000")
PRODUCER_ID = os.getenv("NEXUS_PRODUCER_ID", "PRODUCER-A")

mcp = FastMCP("youshop-nexus")
_READ_ONLY = ToolAnnotations(readOnlyHint=True)


async def _get(
    path: str, not_found: dict[str, str], *, params: dict[str, str] | None = None
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            base_url=API_BASE_URL, headers={"X-Producer-Id": PRODUCER_ID}, timeout=5.0
        ) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return not_found
        return {"error": "service_unavailable"}
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        return {"error": "service_unavailable"}
    if isinstance(data, dict):
        return data
    return {"error": "service_unavailable"}


@mcp.tool(annotations=_READ_ONLY)
async def get_order(order_id: str) -> dict[str, Any]:
    """Consulta o pedido, o pagamento, o acesso e o prazo de entrega."""
    return await _get(
        f"/orders/{quote(order_id, safe='')}",
        {"error": "order_not_found", "order_id": order_id},
    )


@mcp.tool(annotations=_READ_ONLY)
async def get_journey(order_id: str) -> dict[str, Any]:
    """Consulta a linha do tempo de eventos e o acesso do pedido."""
    return await _get(
        f"/orders/{quote(order_id, safe='')}/journey",
        {"error": "order_not_found", "order_id": order_id},
    )


@mcp.tool(annotations=_READ_ONLY)
async def find_related_orders(order_id: str) -> dict[str, Any]:
    """Busca pedidos próximos do mesmo produto ou integração, com eventos e acesso."""
    return await _get(
        f"/orders/{quote(order_id, safe='')}/related",
        {"error": "order_not_found", "order_id": order_id},
    )


@mcp.tool(annotations=_READ_ONLY)
async def get_related_tickets(order_ids: list[str]) -> dict[str, Any]:
    """Lista os tickets de suporte dos pedidos informados."""
    return await _get(
        "/tickets",
        {"error": "tickets_not_found"},
        params={"order_ids": ",".join(order_ids)},
    )


@mcp.tool(annotations=_READ_ONLY)
async def get_delivery_policy(product_id: str) -> dict[str, Any]:
    """Consulta o prazo e a regra de elegibilidade do produto."""
    return await _get(
        f"/products/{quote(product_id, safe='')}/policy",
        {"error": "product_not_found", "product_id": product_id},
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
