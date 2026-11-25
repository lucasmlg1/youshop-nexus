from mcp.server.fastmcp import FastMCP

mcp = FastMCP("youshop-nexus")


@mcp.tool()
async def get_order(order_id: str) -> dict:
    """Consulta um pedido pelo identificador."""
    if order_id == "ORD-101":
        return {"order_id": "ORD-101", "status": "shipped"}
    return {"error": "order_not_found"}


if __name__ == "__main__":
    mcp.run(transport="stdio")
