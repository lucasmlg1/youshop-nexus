import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent


async def main() -> None:
    server_params = StdioServerParameters(
        command=sys.executable, args=["app/mcp_server.py"]
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            for tool in tools.tools:
                print(f"Tool: {tool.name}")
                print("Schema de entrada:")
                print(json.dumps(tool.inputSchema, indent=2, ensure_ascii=False))
                if tool.outputSchema is not None:
                    print("Schema de saída:")
                    print(json.dumps(tool.outputSchema, indent=2, ensure_ascii=False))

            for order_id in ("ORD-101", "ORD-999"):
                result = await session.call_tool("get_order", {"order_id": order_id})
                print(f"\nResposta para {order_id}:")
                for block in result.content:
                    print(block.text if isinstance(block, TextContent) else block)


if __name__ == "__main__":
    asyncio.run(main())
