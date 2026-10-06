"""Bounded, transparent fallback for searches with no exact listing matches."""

from collections.abc import Awaitable, Callable
from typing import Any


SearchTool = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _monthly_cost(listing: dict[str, Any], occupants: int | None) -> int:
    price = int(listing["price"])
    if listing.get("priceUnit") == "PERSON_MONTH" and occupants:
        return price * occupants
    return price


def _differences(listing: dict[str, Any], filters: dict[str, Any]) -> list[str]:
    notes = []
    occupants = filters.get("min_occupants") or None
    maximum = filters.get("price_max") or None
    if occupants and listing.get("maxOccupants") is not None and listing["maxOccupants"] < occupants:
        notes.append(f"chỉ ghi tối đa {listing['maxOccupants']} người, chưa đủ {occupants} người")
    if maximum and _monthly_cost(listing, occupants) > maximum:
        if listing.get("priceUnit") == "PERSON_MONTH" and occupants:
            cost = _monthly_cost(listing, occupants)
            formatted_cost = f"{cost:,}".replace(",", ".")
            notes.append(f"giá cho {occupants} người khoảng {formatted_cost} đ/tháng, vượt ngân sách")
        else:
            notes.append("giá thuê vượt ngân sách")
    if filters.get("has_mezzanine") and listing.get("hasMezzanine") is False:
        notes.append("không ghi nhận có gác")
    if filters.get("has_balcony") and listing.get("hasBalcony") is False:
        notes.append("không ghi nhận ban công riêng")
    if filters.get("has_parking") and listing.get("category") == "ROOM":
        if listing.get("parkingPolicy") not in {"FREE", "PAID"} or (listing.get("maxVehicles") or 0) <= 0:
            notes.append("chưa xác nhận được chỗ gửi xe")
    if filters.get("has_garage") is True and listing.get("hasGarage") is not True:
        notes.append("không ghi nhận gara")
    if filters.get("has_garage") is False and listing.get("hasGarage") is not False:
        notes.append("có gara, khác yêu cầu không gara")
    return notes


def _rank_penalty(item: dict[str, Any]) -> int:
    penalty = 0
    for note in item["matchNotes"]:
        if "vượt ngân sách" in note:
            penalty += 4
        elif "chỉ ghi tối đa" in note:
            penalty += 3
        elif "chưa xác nhận gần" in note:
            penalty += 2
        else:
            penalty += 2
    return penalty + (0 if item["matchScope"] == "same_landmark"
                      else 1 if item["matchScope"] == "same_area" else 4)


async def find_related_listings(
    filters: dict[str, Any], search_tool: SearchTool,
) -> dict[str, Any]:
    """Preserve listing type and locality before considering a wider city search.

    The listing database has no coordinates; results are related to named POIs
    by indexed listing text, not by a measured walking distance.
    """
    base = {**filters, "price_max": 0, "min_occupants": 0,
            "has_mezzanine": False, "has_balcony": False,
            "has_parking": False, "has_garage": None,
            "listing_ids": [], "page": 1, "size": 20, "sort": "price_asc"}
    tiers: list[tuple[str, dict[str, Any]]] = []
    if filters.get("landmark"):
        tiers.append(("same_landmark", base))
    if filters.get("district") or filters.get("location"):
        tiers.append(("same_area", {**base, "landmark": ""}))
    province_query = {**base, "landmark": "", "district": "", "location": ""}

    seen: set[str] = set()
    candidates = []
    for tier, query in tiers:
        result = await search_tool("search_listings", query)
        for item in result.get("matches") or []:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            notes = _differences(item, filters)
            if tier == "same_area" and filters.get("landmark"):
                notes.append("cùng khu vực, chưa xác nhận gần địa điểm bạn nêu")
            candidate = {**item, "matchNotes": notes, "matchScope": tier}
            candidates.append(candidate)
    if not candidates:
        result = await search_tool("search_listings", province_query)
        for item in result.get("matches") or []:
            if item["id"] in seen:
                continue
            notes = _differences(item, filters)
            notes.append("chỉ cùng tỉnh/thành phố; chưa xác nhận khoảng cách tới địa điểm bạn nêu")
            candidates.append({**item, "matchNotes": notes, "matchScope": "same_province"})
    if not candidates:
        return {"total": 0, "matches": [], "isApproximate": False}
    candidates.sort(key=lambda item: (
        _rank_penalty(item),
        _monthly_cost(item, filters.get("min_occupants") or None),
        item["id"],
    ))
    scopes = {item["matchScope"] for item in candidates[:5]}
    return {
        "total": 0,
        "matches": candidates[:5],
        "isApproximate": True,
        "matchTier": scopes.pop() if len(scopes) == 1 else "mixed",
    }
