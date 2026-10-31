import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from homespace_ai.application.listing_context import context_for
from homespace_ai.application.nearby_listing_advice import radius_for, answer_nearby
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.api.v1.agent_ask import stored_answer
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner, ToolPlan
from homespace_ai.clients.generative import GenerationUnavailableError
from homespace_ai.tools.listings import ListingTools, ListingToolError
from homespace_ai.application import renter_search_state as rs
from tests.unit.listing_fakes import FakeTools, c, make_assistant, search
from tests.unit.test_listing_tool_planner import CAT, TODAY, config, call

IDS = [f"00000000-0000-0000-0000-{i:012d}" for i in range(1, 11)]
HISTORY = [{"role": "assistant", "content": "Mình tìm được 10 phòng.", "listingContext": context_for(IDS)}]

class NearbyTools(FakeTools):
    def __init__(self, complete=True, enabled=True):
        super().__init__(IDS)
        self.complete, self.enabled = complete, enabled
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
    a.tools = NearbyTools(complete, enabled)
    return a


@pytest.mark.asyncio
async def test_t15_hard_criteria_are_rechecked_in_scope_before_showing_all_eligible_columns():
    a = assistant()
    a.tools.ids = IDS[:5]  # Only the first five still satisfy the stored hard criteria in Core.
    state = rs.merge(None, {"constraints": [{"field": "amenity.ELEVATOR", "op": "eq", "value": True, "strength": "hard"}]}, "")
    result = await a.try_answer("Trong các phòng này, tiện ích xung quanh thì nên chọn phòng nào?", HISTORY, "t15", state=state)
    check = a.tools.payloads[0]
    assert check["scopeIds"] == IDS and {"field": "amenity.ELEVATOR", "op": "eq", "value": "true"} in check["where"]["all"]
    assert result.listing_ids == IDS[:5], "Failed hard criteria are excluded; eligible columns keep their original order"
    assert a.tools.read == result.listing_ids
    assert len(a.generative_client.boundary_calls[0]["mainListings"]) == 5


@pytest.mark.asyncio
async def test_t15_no_eligible_room_in_scope_is_no_results_not_a_nearby_pick():
    a = assistant()
    a.tools.ids = []
    state = rs.merge(None, {"constraints": [{"field": "amenity.ELEVATOR", "op": "eq", "value": True, "strength": "hard"}]}, "")
    result = await a.try_answer("Trong các phòng này, tiện ích xung quanh thì nên chọn phòng nào?", HISTORY, "t15-empty", state=state)
    assert result.status == "NO_RESULTS" and not result.listing_ids and not a.tools.read
    assert a.generative_client.kwargs is None

@pytest.mark.asyncio
async def test_default_advice_uses_seven_collected_groups_and_preserves_all_columns():
    a = assistant()
    result = await answer_nearby(a, "Tiện ích xung quanh thì nên chọn phòng nào?", HISTORY, "combined",
                                {"listingIds": IDS, "radiusMeters": 2000})
    assert result.status == "ANSWERED" and result.listing_ids == IDS
    assert len(a.tools.nearby_calls[0][2]) == 7
    assert "SPORTS" not in a.tools.nearby_calls[0][2]

@pytest.mark.asyncio
async def test_uncollected_sports_is_unknown_and_does_not_support_a_recommendation():
    a = assistant()
    nearby = a.tools.nearby
    async def not_collected(*args):
        result = await nearby(*args)
        for summary in result["summaries"]:
            for group in summary["groups"]:
                group.update(coverageStatus="NOT_REQUESTED", nearest=[], knownCount=0, radiusCoveredMeters=None)
        return result
    a.tools.nearby = not_collected
    result = await answer_nearby(a, "Phòng nào gần phòng gym thì nên chọn?", HISTORY, "sports",
                                {"listingIds": IDS, "radiusMeters": 2000, "groups": ["SPORTS"]})
    assert "Chưa có thông tin cho nhóm này" in result.answer and result.listing_ids == IDS
    assert not any(path.startswith("nearby.") for path in a.generative_client.boundary_calls[0]["factMatrix"])
    assert a.generative_client.kwargs is None

@pytest.mark.asyncio
async def test_limited_hundred_result_sample_keeps_positive_evidence_without_inventing_missing_schools():
    a = assistant()
    original = a.tools.nearby
    async def limited(*args):
        evidence = await original(*args)
        for summary in evidence["summaries"]:
            for group in summary["groups"]:
                group["coverageStatus"] = "LIMITED_PROVIDER_RESPONSE"
                if group["code"] == "EDUCATION":
                    group.update(nearest=[], knownCount=0)
        return evidence
    a.tools.nearby = limited
    result = await a.try_answer("Trong các phòng này, tiện ích xung quanh thì nên chọn phòng nào?", HISTORY, "single-request")
    assert len(result.listing_ids) == len(a.tools.read) == 10
    evidence = a.generative_client.boundary_calls[0]
    assert "nearby.SHOPPING.distanceMeters" in evidence["factMatrix"]
    assert "nearby.EDUCATION.distanceMeters" not in evidence["factMatrix"]
    assert "mỗi nhóm hiển thị tối đa 5 địa điểm" in result.answer

@pytest.mark.asyncio
async def test_limited_sample_with_no_school_evidence_cannot_claim_a_school_recommendation():
    a = assistant()
    original = a.tools.nearby
    async def no_evidence(*args):
        evidence = await original(*args)
        for summary in evidence["summaries"]:
            for group in summary["groups"]:
                group.update(coverageStatus="LIMITED_PROVIDER_RESPONSE", nearest=[], knownCount=0)
        return evidence
    a.tools.nearby = no_evidence
    result = await answer_nearby(a, "Phòng nào gần trường học thì nên chọn?", HISTORY, "unknown-school",
                                {"listingIds": IDS, "radiusMeters": 2000, "groups": ["EDUCATION"]})
    assert result.listing_ids == IDS and "Chưa có thông tin cho nhóm này" in result.answer
    assert not any(path.startswith("nearby.") for path in a.generative_client.boundary_calls[0]["factMatrix"])
    assert a.generative_client.kwargs is None

@pytest.mark.asyncio
async def test_scoped_ten_use_bounded_cached_summary_without_reordering_or_losing_ordinals():
    a = assistant()
    result = await a.try_answer("Trong các phòng này, tiện ích xung quanh thuận tiện thì nên chọn phòng nào?", HISTORY, "nearby")
    assert result.status == "ANSWERED"
    assert result.listing_ids == a.tools.read == IDS
    evidence = a.generative_client.boundary_calls[0]
    assert len(evidence["mainListings"]) == 10
    assert "3 km" in result.answer and "đường chim bay" not in result.answer
    assert result.listing_context["listingIds"] == IDS
    assert result.listing_positions[IDS[9]] == 10
    assert result.listing_context["nearby"]["radiusMeters"] == 3000
    assert result.data_sources == ["CORE_NEARBY"]

@pytest.mark.asyncio
async def test_five_km_request_cannot_be_silently_answered_as_three_or_rank_partial_coverage():
    a = assistant(complete=False)
    result = await a.try_answer("Trong các phòng này, bán kính 5 km thì nên chọn phòng nào?", HISTORY, "partial")
    assert a.tools.nearby_calls[0][1] == 5000
    assert result.listing_ids == IDS
    assert "5 km" in result.answer and "Dữ liệu là mẫu" not in result.answer
    assert not any(path.startswith("nearby.") for path in a.generative_client.boundary_calls[0]["factMatrix"])
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
    assert result.listing_ids == IDS  # The target affects advice facts, never the table's ordinal mapping.
    assert "nearby.target.distanceMeters" in a.generative_client.boundary_calls[0]["factMatrix"]
    assert a.tools.nearby_calls[0][3] == IDS[0]

@pytest.mark.asyncio
async def test_followup_keeps_radius_and_target_references_without_old_poi_facts():
    a = assistant()
    history = [{"role": "assistant", "content": "Tra cứu tiện ích được đọc lại khi cần.",
                "listingContext": context_for(IDS) | {"nearby": {"radiusMeters": 5000, "groups": ["SHOPPING"], "targetPlaceId": None}}}]
    result = await a.try_answer("Vậy nên chọn phòng nào trong các phòng này?", history, "memory")
    assert a.planner.history is None
    assert a.tools.nearby_calls[0][1:3] == (5000, ["SHOPPING"])
    assert result.listing_ids == IDS

@pytest.mark.asyncio
async def test_native_nearby_registry_is_feature_gated_and_rejects_unknown_selection(monkeypatch):
    settings = config()
    assert settings.nearby_query_enabled is True  # The shipped configuration enables cached POI tools.
    planner = ListingToolPlanner(settings)
    async def post(payload):
        names = {t["name"] for t in payload["tools"]}
        assert "get_listing_nearby_places" in names and "find_listings_near_place" not in names  # place search is inside v2 search_listings
        return call("get_listing_nearby_places", {"listingIds": [IDS[0]], "radiusMeters": 5000, "groups": ["SHOPPING"], "placeQuery": None})
    monkeypatch.setattr(planner, "_post", post)
    result = await planner.plan("Tiện ích xung quanh phòng đó", HISTORY, CAT, None, TODAY)
    assert result.name == "get_listing_nearby_places"
    with pytest.raises(GenerationUnavailableError):
        await planner.plan("Tiện ích xung quanh", [], CAT, None, TODAY)

@pytest.mark.asyncio
async def test_near_place_search_keeps_filters_and_requested_count_without_landmark_text():
    target = {"placeId": "11111111-1111-1111-1111-111111111111", "name": "Đại Học Công Nghiệp", "address": ""}
    a = make_assistant(search(c("price_amount", "lte", "4000000"), place="Đại học Công nghiệp", radius=3000,
                              keyword="Đại học Công nghiệp", sort="price_asc", limit=5), ids=IDS)
    a.tools.places = {"Đại học Công nghiệp": [target]}
    await a.try_answer("Tìm 10 phòng gần Đại học Công nghiệp, tối đa 4 triệu", [], "near")
    payload = a.tools.payloads[0]
    assert payload["limit"] == 10 and "keyword" not in payload
    assert payload["nearbyAll"] == [{"placeId": target["placeId"], "radiusMeters": 3000}]
    assert {"field": "rent.monthly_amount", "op": "lte", "value": "4000000"} in payload["where"]["all"]

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


@pytest.mark.asyncio
async def test_batch_requests_five_pois_per_group_and_render_keeps_all_five_names_without_distances():
    from homespace_ai.application.nearby_table import render
    from tests.unit.listing_fakes import room
    summaries = (await NearbyTools().nearby(IDS[:4], 2000, ["EDUCATION", "SHOPPING"]))["summaries"]
    for index, summary in enumerate(summaries):
        for group in summary["groups"]:
            group["nearest"] = [{"id": f"poi-{index}-{n}", "name": f"{group['code']} phòng {index+1} điểm {n+1}",
                                 "distanceMeters": 100+n} for n in range(5)]
    gateway = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"result": summaries})))
    tools = ListingTools(gateway, None)
    data = await tools.nearby(IDS[:4], 2000, ["EDUCATION", "SHOPPING"])
    gateway.request.assert_awaited_once()
    assert gateway.request.await_args.kwargs["json"]["nearestPerGroup"] == 5
    text = render(data["summaries"], [room(i) for i in IDS[:4]], IDS[:4], 2000)
    for group in ("EDUCATION", "SHOPPING"):
        for room_number in range(1, 5):
            for point in range(1, 6):
                assert f"{group} phòng {room_number} điểm {point}" in text
    assert text.count("<br>") == 32
    assert "100 m" not in text and "đường chim bay" not in text and "TomTom" not in text
    assert "phạm vi 2 km" in text


@pytest.mark.asyncio
async def test_six_pois_in_a_group_is_rejected_instead_of_silently_increasing_payload():
    summaries = (await NearbyTools().nearby(IDS[:1], 2000, ["EDUCATION"]))["summaries"]
    summaries[0]["groups"][0]["nearest"] *= 6
    gateway = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"result": summaries})))
    with pytest.raises(ListingToolError):
        await ListingTools(gateway, None).nearby(IDS[:1], 2000, ["EDUCATION"])


def test_provider_names_cannot_inject_extra_table_rows_links_or_html():
    from homespace_ai.application.nearby_table import render
    from tests.unit.listing_fakes import room
    result = render([{"listingId": IDS[0], "groups": [{"code": "EDUCATION", "nearest": [
        {"name": "Trường A | <br><img src=x onerror=alert(1)> [click](javascript:alert(1))", "distanceMeters": 100}]}]}],
        [room(IDS[0])], IDS[:1], 2000)
    assert "<img" not in result and "<br>" not in result and "\\[click\\]" in result
    assert result.count("| Giáo dục |") == 1
