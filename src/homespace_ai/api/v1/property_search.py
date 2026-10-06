"""Public natural-language listing search; the Gateway remains the public entry point."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from homespace_ai.core.security import verify_gateway_secret
from homespace_ai.property_search.intent import parse_intent
from homespace_ai.property_search.repository import ListingFilters
from homespace_ai.property_search.mcp_client import call_listing_tool

router = APIRouter(prefix="/property-search", tags=["Property search"], dependencies=[Depends(verify_gateway_secret)])


class PropertySearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    provinceCode: str = Field(min_length=1, max_length=4)
    district: str | None = Field(default=None, max_length=100)
    category: str | None = None
    page: int = Field(default=1, ge=1, le=100)
    size: int = Field(default=10, ge=1, le=20)
    sort: str = "newest"
    hasVideo: bool = False


@router.post("")
async def search_properties(body: PropertySearchRequest) -> dict:
    intent = await parse_intent(body.query)
    resolved = await call_listing_tool("resolve_listing_ward", {
        "province_code": body.provinceCode,
        "query": body.query,
    })
    if resolved.get("ward"):
        intent.location = resolved["ward"]
    filters = ListingFilters(
        province_code=body.provinceCode,
        district=body.district,
        category=body.category if body.category in {"ROOM", "APARTMENT", "HOUSE"} else intent.category,
        price_max=intent.price_max,
        has_mezzanine=intent.has_mezzanine,
        has_balcony=intent.has_balcony,
        has_parking=intent.has_parking,
        has_garage=intent.has_garage,
        has_video=body.hasVideo,
        location=intent.location,
        landmark=intent.landmark,
        page=body.page,
        size=body.size,
        sort=body.sort,
    )
    data = await call_listing_tool("search_listings", {
        "province_code": filters.province_code,
        "district": filters.district or "",
        "category": filters.category or "",
        "price_max": filters.price_max or 0,
        "has_mezzanine": filters.has_mezzanine,
        "has_balcony": filters.has_balcony,
        "has_parking": filters.has_parking,
        "has_garage": filters.has_garage,
        "has_video": filters.has_video,
        "location": filters.location or "",
        "landmark": filters.landmark or "",
        "page": filters.page,
        "size": filters.size,
        "sort": filters.sort,
    })
    return {**data, "intent": intent.model_dump(), "query": body.query}


@router.get("/suggestions")
async def property_suggestions(
    provinceCode: str = Query(min_length=1, max_length=4),
    district: str | None = Query(default=None, max_length=100),
) -> dict:
    try:
        return await call_listing_tool("suggest_listing_places", {
            "province_code": provinceCode,
            "district": district or "",
        })
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Không thể tải gợi ý địa điểm.") from exc
