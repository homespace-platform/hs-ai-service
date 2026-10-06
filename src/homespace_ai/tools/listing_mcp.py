"""Standalone MCP server for read-only, live HomeSpace listing discovery.

Run from hs-ai-service: uv run python -m homespace_ai.tools.listing_mcp
The AI API uses the same bounded query functions as these MCP tools.
"""

from mcp.server import MCPServer

from homespace_ai.property_search.repository import ListingFilters, search_public_listings, suggest_places, match_listing_ward
from homespace_ai.property_search.details import get_public_listing_facts

mcp = MCPServer("homespace-listing-search")


@mcp.tool()
async def get_listing_facts(listing_ids: list[str]) -> dict:
    """Read all public fields of up to 20 active published listings by exact ID.

    Returns common, category-specific, address, charges, amenities, furnishing,
    viewing days/slots and media facts from the live HomeSpace database.
    """
    return await get_public_listing_facts(listing_ids)


@mcp.tool()
async def search_listings(
    province_code: str,
    district: str = "",
    category: str = "",
    price_max: int = 0,
    min_occupants: int = 0,
    has_mezzanine: bool = False,
    has_balcony: bool = False,
    has_parking: bool = False,
    has_garage: bool | None = None,
    has_video: bool = False,
    location: str = "",
    landmark: str = "",
    listing_ids: list[str] | None = None,
    page: int = 1,
    size: int = 10,
    sort: str = "newest",
) -> dict:
    """Search only active published listings with structured filters; no arbitrary SQL."""
    return await search_public_listings(ListingFilters(
        province_code=province_code,
        district=district or None,
        category=category or None,
        price_max=price_max or None,
        min_occupants=min_occupants or None,
        has_mezzanine=has_mezzanine,
        has_balcony=has_balcony,
        has_parking=has_parking,
        has_garage=has_garage,
        has_video=has_video,
        location=location or None,
        landmark=landmark or None,
        listing_ids=listing_ids or None,
        page=page,
        size=size,
        sort=sort,
    ))


@mcp.tool()
async def suggest_listing_places(province_code: str, district: str = "") -> dict:
    """Suggest curated landmarks/POIs; fall back to the province when a district has no curated match."""
    suggestions = await suggest_places(province_code, district or None)
    if suggestions or not district:
        return {"suggestions": suggestions, "fallbackToProvince": False}
    return {
        "suggestions": await suggest_places(province_code),
        "fallbackToProvince": True,
    }


@mcp.tool()
async def resolve_listing_ward(province_code: str, query: str) -> dict:
    """Match a place named in natural language to a ward present in live listings."""
    return {"ward": await match_listing_ward(province_code, query[:500])}


if __name__ == "__main__":
    mcp.run(transport="stdio")
