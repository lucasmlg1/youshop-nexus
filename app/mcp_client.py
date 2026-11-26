"""Cliente MCP (stdio), sanitização de logs e verificação determinística de recuperação."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from .validator import dedupe_preserve_order

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_PRODUCER_ID = "PRODUCER-A"
MCP_SERVER_PATH = Path(__file__).resolve().parent / "mcp_server.py"


@asynccontextmanager
async def mcp_session(
    *,
    producer_id: str = DEFAULT_PRODUCER_ID,
    api_url: str | None = None,
) -> AsyncIterator[ClientSession]:
    """Inicia o servidor MCP via stdio isolando o escopo do produtor no ambiente."""
    # Princípio do menor privilégio: passamos apenas URL da API e produtor autorizado,
    # sem expor GEMINI_API_KEY ao subprocesso do servidor MCP.
    env = {
        "NEXUS_API_URL": api_url or os.getenv("NEXUS_API_URL", DEFAULT_API_URL),
        "NEXUS_PRODUCER_ID": producer_id,
    }
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(MCP_SERVER_PATH)],
        env=env,
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def extract_tool_payload(result: Any) -> dict[str, Any]:
    """Normaliza o retorno de session.call_tool para um dicionário JSON."""
    if result.isError:
        return {"error": "tool_execution_error"}
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for block in result.content:
        if isinstance(block, TextContent):
            try:
                parsed = json.loads(block.text)
                if isinstance(parsed, dict):
                    return parsed
                return {"data": parsed}
            except ValueError:
                return {"text": block.text}
    return {"error": "empty_tool_response"}


def sanitize_for_log(value: Any, *, max_str_len: int = 300) -> Any:
    """Limita strings longas para manter o log de auditoria enxuto e seguro."""
    if isinstance(value, str):
        return value if len(value) <= max_str_len else value[:max_str_len] + "..."
    if isinstance(value, list):
        return [sanitize_for_log(item, max_str_len=max_str_len) for item in value]
    if isinstance(value, dict):
        return {
            str(k): sanitize_for_log(v, max_str_len=max_str_len)
            for k, v in value.items()
        }
    return value


async def verify_recovery(
    order_ids: list[str],
    *,
    producer_id: str = DEFAULT_PRODUCER_ID,
    api_url: str | None = None,
) -> dict[str, Any]:
    """Reconsulta pedidos afetados via MCP sem usar LLM (checagem determinística — RF05)."""
    unique_ids = dedupe_preserve_order(order_ids)
    regularized: list[str] = []
    pending: list[str] = []
    errors: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []

    async with mcp_session(producer_id=producer_id, api_url=api_url) as session:
        for order_id in unique_ids:
            result = await session.call_tool("get_order", {"order_id": order_id})
            payload = extract_tool_payload(result)
            tool_calls.append(
                {
                    "tool_name": "get_order",
                    "arguments": {"order_id": order_id},
                    "result": sanitize_for_log(payload),
                }
            )
            if payload.get("error"):
                errors.append({"order_id": order_id, "error": payload["error"]})
            elif payload.get("has_access") is True:
                regularized.append(order_id)
            else:
                pending.append(order_id)

    total = len(unique_ids)
    summary = (
        f"{len(regularized)} de {total} pedidos regularizados; "
        f"{len(pending)} ainda pendente(s)"
        + (f" ({', '.join(pending)})." if pending else ".")
    )
    return {
        "checked_order_ids": unique_ids,
        "regularized_order_ids": regularized,
        "pending_order_ids": pending,
        "errors": errors,
        "summary": summary,
        "tool_calls": tool_calls,
    }
