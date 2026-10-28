import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from homespace_ai.application.listing_context import context_for
from homespace_ai.application.nearby_listing_advice import radius_for
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.api.v1.agent_ask import stored_answer
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner, ToolPlan
from homespace_ai.clients.generative import GenerationUnavailableError
from homespace_ai.tools.listings import ListingTools, ListingToolError
from tests.unit.test_listing_ask import make_assistant, FakeTools
from tests.unit.test_listing_tool_planner import config, call

IDS = [f"00000000-0000-0000-0000-{i:012d}" for i in range(1, 11)]
HISTORY = [{"role": "assistant", "content": "Mình tìm được 10 phòng.", "listingContext": context_for(IDS)}]

class NearbyTools(FakeTools):
    def __init__(self, complete=True, enabled=True):
        super().__init__(IDS)
        self.read = []
        self.complete, self.enabled = complete, enabled
        self.nearby_calls = []
    async def detail(self, value):
        self.read.append(value)
        return await super().detail(value)
    async def nearby(self, ids, radius, groups, target=None):
        self.nearby_calls.append((ids, radius, groups, target))
        return {"summaries": [{
            "listingId": value, "enabled": self.enabled, "radiusRequestedMeters": radius,
            "groups": [{"code": code, "coverageStatus": "COMPLETE_PROVIDER_RESPONSE" if self.complete else "PARTIAL",
                        "radiusCoveredMeters": 3000 if not self.complete else radius, "dataAsOf": "2026-10-28T00:00:00Z",
                        "knownCount": 1, "nearest": [{"id": "poi", "name": "Điểm từ fixture", "distanceMeters": 1100 - IDS.index(value) * 100}]}
                       for code in groups],
            **({"target": {"placeId": target, "name": "Đúng cơ sở khách yêu cầu", "distanceMeters": 100 + IDS.index(value) * 10, "withinRadius": True}} if target else {})
        } for value in ids]}

def assistant(complete=True, enabled=True, place_query=None):
    a = make_assistant(ToolPlan("get_listing_nearby_places", {"listingIds": IDS, "radiusMeters": 3000,
                                                          "groups": ["EDUCATION", "SHOPPING"], "placeQuery": place_query}))
    a.settings = SimpleNamespace(nearby_query_enabled=True)
    a.tools = NearbyTools(complete, enabled)
    return a

@pytest.mark.asyncio
async def test_scoped_ten_are_ranked_from_cached_summary_but_only_three_details_reach_model():
    a = assistant()
    result = await a.try_answer("Trong các phòng này, tiện ích xung quanh thuận tiện thì nên chọn phòng nào?", HISTORY, "nearby")
    assert result.status == "ANSWERED"
    assert len(result.listing_ids) == len(a.tools.read) == 3
    assert a.tools.read == [IDS[9], IDS[8], IDS[7]]
    evidence = json.loads(a.generative_client.kwargs["context_chunks"][0]["content"])
    assert evidence["distanceType"] == "STRAIGHT_LINE"
    assert len(evidence["nearby"]) == 3
    assert result.listing_context["listingIds"] == IDS
    assert result.listing_positions[IDS[9]] == 10
    assert result.listing_context["nearby"]["radiusMeters"] == 3000
    assert result.data_sources == ["CORE_NEARBY"]

@pytest.mark.asyncio
async def test_five_km_request_cannot_be_silently_answered_as_three_or_rank_partial_coverage():
    a = assistant(complete=False)
    result = await a.try_answer("Trong các phòng này, bán kính 5 km thì nên chọn phòng nào?", HISTORY, "partial")
    assert a.tools.nearby_calls[0][1] == 5000
    assert not result.listing_ids and not a.tools.read
    assert "chưa đủ" in result.answer
    assert result.listing_context["listingIds"] == IDS
    assert a.generative_client.kwargs is None

@pytest.mark.asyncio
async def test_backend_disabled_is_not_interpreted_as_no_nearby_places():
    a = assistant(enabled=False)
    result = await a.try_answer("Quét tiện ích xung quanh các phòng này", HISTORY, "disabled")
    assert "chưa được bật" in result.answer and not a.tools.read
    assert result.listing_context["listingIds"] == IDS

@pytest.mark.asyncio
async def test_exact_school_ambiguity_does_not_turn_into_generic_education_recommendation():
    a = assistant(place_query="Đại học Công nghiệp")
    a.tools.resolve_nearby_search = AsyncMock(return_value={"needsClarification": True, "candidates": [
        {"id": IDS[0], "name": "Cơ sở A"}, {"id": IDS[1], "name": "Cơ sở B"}]})
    result = await a.try_answer("Trong các phòng này, phòng nào gần trường Đại học Công nghiệp hơn?", HISTORY, "school")
    assert "cơ sở nào" in result.answer and "Cơ sở A" in result.answer
    assert not a.tools.read and not a.tools.nearby_calls
    assert result.data_sources == ["CORE_NEARBY"]

@pytest.mark.asyncio
async def test_specific_target_distance_takes_precedence_over_generic_school_density():
    a = assistant(place_query="Đúng trường")
    a.tools.resolve_nearby_search = AsyncMock(return_value={"arguments": {"nearby": {"placeId": IDS[0]}}})
    result = await a.try_answer("Phòng nào gần trường Đại học của mình nhất?", HISTORY, "target")
    assert result.listing_ids == IDS[:3]  # Specific target ordering is opposite the generic group fixture.
    assert a.tools.nearby_calls[0][3] == IDS[0]

@pytest.mark.asyncio
async def test_followup_keeps_radius_and_target_references_without_old_poi_facts():
    a = assistant()
    history = [{"role": "assistant", "content": "Tra cứu tiện ích được đọc lại khi cần.",
                "listingContext": context_for(IDS) | {"nearby": {"radiusMeters": 5000, "groups": ["SHOPPING"], "targetPlaceId": None}}}]
    result = await a.try_answer("Vậy nên chọn phòng nào trong các phòng này?", history, "memory")
    assert a.planner.history is None
    assert a.tools.nearby_calls[0][1:3] == (5000, ["SHOPPING"])
    assert len(result.listing_ids) == 3

@pytest.mark.asyncio
async def test_native_nearby_registry_is_feature_gated_and_rejects_unknown_selection(monkeypatch):
    settings = config()
    assert settings.nearby_query_enabled is True  # The shipped configuration enables cached POI tools.
    planner = ListingToolPlanner(settings)
    async def post(payload):
        names = {t["name"] for t in payload["tools"]}
        assert "get_listing_nearby_places" in names and "find_listings_near_place" in names
        return call("get_listing_nearby_places", {"listingIds": [IDS[0]], "radiusMeters": 5000, "groups": ["SHOPPING"], "placeQuery": None})
    monkeypatch.setattr(planner, "_post", post)
    result = await planner.plan("Tiện ích xung quanh phòng đó", HISTORY, {"category": "TEXT"})
    assert result.name == "get_listing_nearby_places"
    with pytest.raises(GenerationUnavailableError):
        await planner.plan("Tiện ích xung quanh", [], {"category": "TEXT"})

@pytest.mark.asyncio
async def test_near_place_search_keeps_filters_and_requested_count_without_landmark_text(monkeypatch):
    settings = config(); settings.nearby_query_enabled = True
    planner = ListingToolPlanner(settings)
    async def post(payload):
        return call("find_listings_near_place", {"placeQuery": "Đại học Công nghiệp", "group": None, "radiusMeters": 3000,
            "filters": [{"field": "price_amount", "op": "lte", "value": "4000000"}], "sort": "price_asc", "limit": 5})
    monkeypatch.setattr(planner, "_post", post)
    plan = await planner.plan("Tìm 10 phòng gần Đại học Công nghiệp, tối đa 4 triệu", [], {"price_amount": "NUMBER", "category": "TEXT"})
    assert plan.arguments["limit"] == 10
    assert "keyword" not in plan.arguments
    assert {"field": "price_amount", "op": "lte", "value": "4000000"} in plan.arguments["filters"]

@pytest.mark.parametrize("text,expected", [("Trong bán kính 5 km", 5000), ("Phạm vi 500 m", 500), ("Phòng rộng 30m, gần siêu thị", 2000)])
def test_radius_uses_meters_not_area(text, expected):
    assert radius_for(text, []) == expected

def test_expiring_provider_prose_is_not_copied_to_mongo_or_guest_jwt():
    result = AskResult(answer="POI-name, distance 123 m", status="ANSWERED", citations=[], request_id="retention",
                       data_sources=["CORE_NEARBY"])
    assert "POI-name" not in stored_answer(result) and "123" not in stored_answer(result)
    result.data_sources = []
    assert stored_answer(result) == result.answer

@pytest.mark.asyncio
async def test_changed_location_priority_is_planned_again_instead_of_reusing_old_groups():
    a = assistant()
    history = [{"role": "assistant", "content": "Tra cứu tiện ích được đọc lại khi cần.",
                "listingContext": context_for(IDS) | {"nearby": {"radiusMeters": 3000, "groups": ["SHOPPING"], "targetPlaceId": None}}}]
    await a.try_answer("Trong các phòng này, nên chọn phòng nào gần bệnh viện và trường học hơn?", history, "changed")
    assert a.planner.history is not None
    assert a.tools.nearby_calls[0][2] == ["EDUCATION", "SHOPPING"]  # Fresh planner fixture, not saved SHOPPING-only preference.

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing_group", "too_many_pois", "nan_distance"])
async def test_tool_rejects_malformed_or_unbounded_nearby_payload(bad):
    summary = (await NearbyTools().nearby(IDS[:1], 3000, ["EDUCATION"]))["summaries"]
    if bad == "missing_group":
        summary[0]["groups"] = []
    elif bad == "too_many_pois":
        summary[0]["groups"][0]["nearest"] *= 1000
    else:
        summary[0]["groups"][0]["nearest"][0]["distanceMeters"] = float("nan")
    gateway = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"result": summary})))
    tools = ListingTools(gateway, None)
    with pytest.raises(ListingToolError):
        await tools.nearby(IDS[:1], 3000, ["EDUCATION"])

@pytest.mark.asyncio
async def test_tool_rejects_more_than_twenty_candidates_before_any_request():
    gateway = SimpleNamespace(request=AsyncMock())
    tools = ListingTools(gateway, None, max_results=50)
    with pytest.raises(ListingToolError):
        await tools.nearby([f"00000000-0000-0000-0000-{i:012d}" for i in range(21)], 3000, ["EDUCATION"])
    gateway.request.assert_not_called()
