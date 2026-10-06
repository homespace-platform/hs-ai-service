"""Invoke the same MCP tools exposed to external AI hosts, in-process for low latency."""

from mcp import Client

from homespace_ai.tools.listing_mcp import mcp


async def call_listing_tool(name: str, arguments: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
    if result.is_error:
        raise RuntimeError(f"Listing MCP tool {name} failed")
    structured = result.structured_content
    if isinstance(structured, dict):
        return structured
    # MCP SDK also emits a text representation for clients without structured output.
    import json

    for block in result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise RuntimeError(f"Listing MCP tool {name} returned no data")
