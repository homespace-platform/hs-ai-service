import json

import pytest

from homespace_ai.clients.listing_tool_planner import ListingToolPlanner, ToolPlan


class Settings:
    generation_provider = "groq"
    generation_fallback_provider = "gemini"
    generation_model = ""
    groq_model = "test-groq"
    gemini_model = "test-gemini"
    groq_api_key = "test"
    gemini_api_key = "test"
    generation_timeout_seconds = 3.0


@pytest.mark.asyncio
async def test_groq_native_tool_call_uses_field_catalog(monkeypatch):
    planner = ListingToolPlanner(Settings())
    async def fake_post(url, payload, headers):
        search = payload["tools"][0]["function"]["parameters"]
        assert search["properties"]["filters"]["items"]["properties"]["field"]["enum"] == ["room.parking_policy"]
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "search_listings", "arguments": json.dumps({"filters": [
                {"field": "room.parking_policy", "op": "eq", "value": "PAID"}
            ]})}}]}}]}
    monkeypatch.setattr(planner, "_post", fake_post)
    result = await planner.plan("Tìm phòng gửi xe có phí", [], {"room.parking_policy": "TEXT"})
    assert result.name == "search_listings"
    assert result.arguments["filters"][0]["value"] == "PAID"


@pytest.mark.asyncio
async def test_gemini_native_tool_call(monkeypatch):
    settings = Settings()
    settings.generation_provider = "gemini"
    settings.generation_fallback_provider = ""
    planner = ListingToolPlanner(settings)
    async def fake_post(url, payload, headers):
        assert payload["tools"][0]["functionDeclarations"][0]["name"] == "search_listings"
        return {"candidates": [{"content": {"parts": [{"functionCall": {
            "name": "get_listing_detail", "args": {"listingId": "00000000-0000-0000-0000-000000000001"}
        }}]}}]}
    monkeypatch.setattr(planner, "_post", fake_post)
    result = await planner.plan("Căn này có máy lạnh không?", [], {"amenity.AIR_CONDITIONER": "BOOLEAN"})
    assert result.name == "get_listing_detail"


@pytest.mark.asyncio
async def test_normalizes_real_room_question_without_inventing_paid_parking(monkeypatch):
    planner = ListingToolPlanner(Settings())
    async def fake_post(url, payload, headers):
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "search_listings", "arguments": json.dumps({"filters": [
                {"field": "ward_name", "op": "eq", "value": "Phú Lợi"},
                {"field": "room.balcony_type", "op": "contains", "value": "ban công"},
                {"field": "charge.MOTORBIKE_PARKING.included_in_rent", "op": "eq", "value": "false"},
            ]})}}]}}]}
    monkeypatch.setattr(planner, "_post", fake_post)
    fields = {field: kind for field, kind in [
        ("ward_name", "TEXT"), ("room.balcony_type", "TEXT"),
        ("room.has_balcony", "BOOLEAN"), ("room.parking_policy", "TEXT"),
        ("category", "TEXT"), ("charge.MOTORBIKE_PARKING.included_in_rent", "BOOLEAN"),
    ]}
    result = await planner.plan(
        "Tìm phòng trọ ở Phường Phú Lợi, Thành phố Hồ Chí Minh có ban công và chỗ gửi xe máy.",
        [], fields,
    )
    assert result.arguments["filters"] == [
        {"field": "ward_name", "op": "eq", "value": "Phường Phú Lợi"},
        {"field": "room.has_balcony", "op": "eq", "value": "true"},
        {"field": "category", "op": "eq", "value": "ROOM"},
        {"field": "room.parking_policy", "op": "ne", "value": "NONE"},
    ]


def test_generic_balcony_does_not_assume_private():
    result = ListingToolPlanner._normalize_search(
        "Tìm phòng trọ có ban công",
        ToolPlan(
            "search_listings", {"filters": [{"field": "room.balcony_type", "op": "contains", "value": "PRIVATE"}]}
        ),
        {"room.balcony_type": "TEXT", "room.has_balcony": "BOOLEAN", "category": "TEXT"},
    )
    assert {"field": "room.has_balcony", "op": "eq", "value": "true"} in result.arguments["filters"]


def test_bare_ward_uses_current_or_prior_full_name():
    plan = ToolPlan("search_listings", {"filters": [{"field": "ward_name", "op": "eq", "value": "Phú Lợi"}]})
    fields = {"ward_name": "TEXT"}
    without_history = ListingToolPlanner._normalize_search("Tìm phòng ở Phú Lợi", plan, fields)
    assert without_history.arguments["filters"] == [
        {"field": "ward_name", "op": "contains", "value": "Phú Lợi"}
    ]
    with_history = ListingToolPlanner._normalize_search("Tìm phòng ở Phú Lợi", plan, fields, [
        {"role": "user", "content": "Tìm phòng trọ tại Phường Phú Lợi, Thành phố Hồ Chí Minh"}
    ])
    assert with_history.arguments["filters"] == [
        {"field": "ward_name", "op": "eq", "value": "Phường Phú Lợi"}
    ]
