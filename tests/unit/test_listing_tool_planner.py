import json

import pytest

from homespace_ai.clients.generative import GenerationUnavailableError
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner, ToolPlan
from homespace_ai.core.config import Settings


def config():
    return Settings(_env_file=None, GENERATION_PROVIDER="openai", OPENAI_API_KEY="test-only-key")


def call(name, args):
    return {"status": "completed", "output": [{"type": "function_call", "name": name,
                                             "arguments": json.dumps(args)}]}


@pytest.mark.asyncio
async def test_openai_native_tool_call_uses_strict_field_catalog(monkeypatch):
    planner = ListingToolPlanner(config())
    async def fake_post(payload):
        assert payload["model"] == "gpt-4.1-mini"
        assert "reasoning" not in payload
        assert payload["store"] is False
        assert payload["parallel_tool_calls"] is False
        search = payload["tools"][0]
        assert search["strict"] is True
        schema = search["parameters"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        item = schema["properties"]["filters"]["items"]
        assert item["additionalProperties"] is False
        assert item["properties"]["field"]["enum"] == ["room.parking_policy"]
        return call("search_listings", {"filters": [
            {"field": "room.parking_policy", "op": "eq", "value": "PAID"}],
            "sort": "newest", "limit": 5})
    monkeypatch.setattr(planner, "_post", fake_post)
    result = await planner.plan("Tìm phòng gửi xe có phí", [], {"room.parking_policy": "TEXT"})
    assert result.name == "search_listings"
    assert result.arguments["filters"][0]["value"] == "PAID"


@pytest.mark.asyncio
async def test_openai_detail_tool_call(monkeypatch):
    planner = ListingToolPlanner(config())
    async def fake_post(payload):
        return call("get_listing_detail", {"listingId": "00000000-0000-0000-0000-000000000001"})
    monkeypatch.setattr(planner, "_post", fake_post)
    assert (await planner.plan("Căn này có máy lạnh không?", [], {})).name == "get_listing_detail"


@pytest.mark.asyncio
async def test_rag_route_without_tool(monkeypatch):
    planner = ListingToolPlanner(config())
    async def fake_post(payload):
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Use RAG"}]}]}
    monkeypatch.setattr(planner, "_post", fake_post)
    assert await planner.plan("HomeSpace là gì?", [], {"category": "TEXT"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"status": "incomplete", "output": []},
    call("invented_tool", {}),
    call("search_listings", {"filters": "bad"}),
    {"status": "completed", "output": [{"type": "function_call", "name": "search_listings",
                                      "arguments": "not JSON"}]},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
    {"status": "completed", "output": [*call("search_listings", {"filters": []})["output"],
                                     *call("search_listings", {"filters": []})["output"]]},
])
async def test_invalid_or_incomplete_tool_call_is_not_routed_to_rag(monkeypatch, response):
    planner = ListingToolPlanner(config())
    async def fake_post(payload):
        return response
    monkeypatch.setattr(planner, "_post", fake_post)
    with pytest.raises(GenerationUnavailableError):
        await planner.plan("Tìm phòng", [], {"category": "TEXT"})


@pytest.mark.asyncio
async def test_normalizes_real_room_question_without_inventing_paid_parking(monkeypatch):
    planner = ListingToolPlanner(config())
    async def fake_post(payload):
        return call("search_listings", {"filters": [
            {"field": "ward_name", "op": "eq", "value": "Phú Lợi"},
            {"field": "room.balcony_type", "op": "contains", "value": "ban công"},
            {"field": "charge.MOTORBIKE_PARKING.included_in_rent", "op": "eq", "value": "false"},
        ]})
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


def test_generic_phong_search_is_room_not_house():
    result = ListingToolPlanner._normalize_search(
        "Tìm phòng khu vực Phú Lợi", ToolPlan("search_listings", {"filters": [
            {"field": "ward_name", "op": "eq", "value": "Phú Lợi"}
        ]}), {"ward_name": "TEXT", "category": "TEXT"})
    assert {"field": "category", "op": "eq", "value": "ROOM"} in result.arguments["filters"]
