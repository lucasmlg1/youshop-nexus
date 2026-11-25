import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def call_tool(session: ClientSession, name: str, **arguments: str | list[str]) -> dict:
    result = await session.call_tool(name, arguments)
    if result.isError or result.structuredContent is None:
        raise RuntimeError(f"Falha ao executar {name}: {result.content}")
    print(f"\n{name}({arguments})")
    print(json.dumps(result.structuredContent, indent=2, ensure_ascii=False))
    return result.structuredContent


async def main() -> None:
    env = {
        name: os.environ[name]
        for name in ("NEXUS_API_URL", "NEXUS_PRODUCER_ID")
        if name in os.environ
    }
    server_params = StdioServerParameters(
        command=sys.executable, args=["app/mcp_server.py"], env=env
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "get_order", "get_journey", "find_related_orders",
                "get_related_tickets", "get_delivery_policy",
            }
            assert all("producer_id" not in tool.inputSchema["properties"] for tool in tools.tools)

            order = await call_tool(session, "get_order", order_id="ORD-101")
            assert order["past_deadline"] is True and order["has_access"] is False

            journey = await call_tool(session, "get_journey", order_id="ORD-101")
            assert any(
                event["event_type"] == "webhook_sent"
                and event["details"]["warning"] == "external_course_id_not_mapped"
                for event in journey["events"]
            )

            related = await call_tool(session, "find_related_orders", order_id="ORD-101")
            candidates = related["related_orders"]
            assert candidates
            assert all(
                order["producer_id"] == "PRODUCER-A" and order["order_id"] != "ORD-111"
                for order in candidates
            )

            for order_id in ("ORD-111", "ORD-999"):
                missing = await call_tool(session, "get_order", order_id=order_id)
                assert missing == {"error": "order_not_found", "order_id": order_id}

            tickets = await call_tool(session, "get_related_tickets", order_ids=["ORD-101", "ORD-102"])
            assert [ticket["ticket_id"] for ticket in tickets["tickets"]] == ["TKT-001"]

            policy = await call_tool(session, "get_delivery_policy", product_id="PROD-01")
            assert policy["deadline_minutes"] == 15


if __name__ == "__main__":
    asyncio.run(main())
