"""Customer wording, fresh cached POIs, follow-up advice and area-scoped boundary calls."""
from copy import deepcopy

import pytest

from homespace_ai.application.amenity_intent import NEARBY_INTENT
from homespace_ai.application.listing_table import requested_sections
from homespace_ai.application import renter_search_state as rs
from homespace_ai.clients.listing_tool_planner import ToolPlan
from tests.unit.listing_fakes import IDS, Conversation, c, make_assistant, room, search


def setup():
    a = make_assistant(ToolPlan("get_listing_details", {"listingIds": IDS[:4]}), IDS[:8])
    groups = ["EDUCATION", "HEALTHCARE", "SHOPPING", "FOOD_DRINK", "FINANCE", "TRANSPORT", "RECREATION"]
    a.tools.summaries = [{"listingId": identifier, "enabled": True, "radiusRequestedMeters": 2000,
        "groups": [{"code": code, "coverageStatus": "LIMITED_PROVIDER_RESPONSE", "radiusCoveredMeters": 2000,
            "knownCount": 4, "nearest": [{"id": IDS[9], "name": "Trường mầm non" if code == "EDUCATION" else "Địa điểm " + code,
                "distanceMeters": 100 + index * 50}]} for code in groups]} for index, identifier in enumerate(IDS[:4])]
    for index, identifier in enumerate(IDS[:8]):
        a.tools.details[identifier] = room(identifier, areaM2=20 + index,
            pricing={"amount": 3000000, "currency": "VND", "unit": "ROOM_MONTH"},
            amenities=[{"code": "WIFI", "name": "WiFi"}])
    a.generative_client.boundary_decision["mainRecommendation"] = {"listingId": IDS[0], "fields": ["areaM2"]}
    state = rs.merge(None, {"constraints": [{"field": "category", "op": "eq", "value": "ROOM", "strength": "hard"}],
                            "context": {"occupants": 2}}, "")
    history = [{"role": "assistant", "content": "4 phòng vừa tìm", "listingContext": a.context(IDS[:4], state)}]
    return a, state, history


@pytest.mark.parametrize("question", [
    "Các phòng trên có những tiện ích nào xung quanh?", "Các phòng đó có tiện ích xung quanh gì?",
    "Xung quanh các phòng này có gì?", "Những phòng này có tiện ích gì ở gần đó?",
    "Quanh các phòng này có những chỗ nào?", "Tiện ích bên ngoài các phòng này ra sao?",
    "Các phòng trên, gần đó có gì?",
])
@pytest.mark.asyncio
async def test_outside_wording_never_uses_building_facilities_even_if_planner_chooses_details(question):
    a, state, history = setup()
    assert NEARBY_INTENT.search(question) and "amenities" not in requested_sections(question)
    result = await a.try_answer(question, history, "wording", state=state)
    assert len(a.tools.nearby_calls) == 1 and not a.tools.value_calls
    assert result.listing_ids == IDS[:4] and result.listing_context["listingIds"] == IDS[:4]
    assert "| Tiện ích xung quanh |" in result.answer and "| Phòng" not in result.answer
    assert all(f"/rent/{identifier}" in result.answer for identifier in IDS[:4])
    assert "Địa điểm SHOPPING" in result.answer and "WiFi" not in result.answer
    assert "Trong nhóm phòng đang xét" in result.answer
    assert a.generative_client.kwargs is None, "POI table is rendered by the app, never model-written"


@pytest.mark.asyncio
async def test_followup_building_facilities_have_a_main_choice_and_a_nearby_question_hint():
    a, state, history = setup()
    result = await a.try_answer("Các phòng trên có những tiện ích tòa nhà nào?", history, "inside", state=state)
    assert not a.tools.nearby_calls and not a.tools.value_calls
    assert "| WiFi |" in result.answer and result.listing_context["mainRecommendation"]["listingId"] == IDS[0]
    assert "Các phòng trên có những tiện ích nào xung quanh?" in result.suggested_questions


@pytest.mark.asyncio
async def test_unspecified_amenities_asks_which_source_and_mixed_request_labels_both():
    a, state, history = setup()
    unclear = await a.try_answer("Các phòng trên có tiện ích gì?", history, "ambiguous", state=state)
    assert unclear.status == "NEEDS_CLARIFICATION" and not a.tools.read and not a.tools.nearby_calls
    mixed = await a.try_answer("Các phòng trên có tiện ích tòa nhà và tiện ích nào xung quanh?", history, "both", state=state)
    assert "**Tiện ích tòa nhà & dịch vụ**" in mixed.answer and "| WiFi |" in mixed.answer
    assert "**Địa điểm bên ngoài**" in mixed.answer and "Địa điểm SHOPPING" in mixed.answer


@pytest.mark.asyncio
async def test_profile_from_a_followup_reaches_the_judge_without_inventing_filters_or_children():
    a, state, history = setup()
    result = await a.try_answer("Mình là sinh viên, các phòng trên có những tiện ích nào xung quanh?", history, "student", state=state)
    evidence = a.generative_client.boundary_calls[-1]
    assert evidence["confirmedCriteria"]["renterProfile"] == {"persona": "STUDENT"}
    assert "nearby.EDUCATION.distanceMeters" not in evidence["factMatrix"], "A kindergarten is not the student's university"
    assert "nearby.SHOPPING.distanceMeters" in evidence["factMatrix"]
    assert result.listing_context["searchState"]["renterProfile"] == {"persona": "STUDENT"}
    assert not evidence["candidates"] and not a.tools.value_calls
    assert a.tools.payloads[-1]["scopeIds"] == IDS[:4]


@pytest.mark.asyncio
@pytest.mark.parametrize("description,persona", [
    ("Mình là người đi làm", "WORKING"), ("Gia đình mình", "FAMILY"), ("Mình làm việc từ xa", "REMOTE"),
])
async def test_renter_profiles_personalize_followup_without_inventing_household_or_transport(description, persona):
    a, state, history = setup()
    result = await a.try_answer(description + ", các phòng trên có những tiện ích nào xung quanh?", history, "profiles", state=state)
    evidence = a.generative_client.boundary_calls[-1]
    assert evidence["confirmedCriteria"]["renterProfile"] == {"persona": persona}
    assert evidence["confirmedCriteria"]["context"] == {"occupants": 2}
    assert evidence["confirmedCriteria"]["constraints"] == state["constraints"]
    assert result.listing_context["mainRecommendation"]["listingId"] in IDS[:4]
    assert not a.tools.value_calls


@pytest.mark.asyncio
async def test_single_main_pick_can_use_fresh_nearby_facts_but_never_persist_provider_facts():
    a, state, history = setup()
    a.generative_client.boundary_decision["mainRecommendation"] = {"listingId": IDS[0], "fields": ["nearby.SHOPPING.distanceMeters"]}
    result = await a.try_answer("Các phòng trên có những tiện ích nào xung quanh?", history, "nearby-pick", state=state)
    assert "Địa điểm SHOPPING — 100 m" in result.answer
    assert result.listing_context["mainRecommendation"] == {"listingId": IDS[0], "fields": ["nearby.SHOPPING.distanceMeters"]}
    assert "Địa điểm SHOPPING" not in str(result.listing_context)
    a.tools.summaries[0]["groups"][2]["nearest"][0]["distanceMeters"] = 150
    history.append({"role": "assistant", "content": "Đã đối chiếu", "listingContext": result.listing_context})
    next_result = await a.try_answer("Vì sao bạn ưu tiên phòng được chọn?", history, "fresh-nearby", state=state)
    assert "Địa điểm SHOPPING — 150 m" in next_result.answer and not a.tools.value_calls


@pytest.mark.asyncio
async def test_missing_poi_evidence_keeps_all_columns_and_does_not_claim_absence_or_rank_by_missing_data():
    a, state, history = setup()
    for summary in a.tools.summaries:
        for group in summary["groups"]:
            group.update(nearest=[], knownCount=0)
    result = await a.try_answer("Các phòng trên có những tiện ích nào xung quanh?", history, "partial", state=state)
    assert result.listing_ids == IDS[:4] and "Chưa ghi nhận trong dữ liệu đã lưu" in result.answer
    assert "không có nghĩa" in result.answer
    assert not any(key.startswith("nearby.") for key in a.generative_client.boundary_calls[-1]["factMatrix"])


@pytest.mark.asyncio
async def test_boundary_runs_once_for_initial_area_then_only_after_ward_or_city_change_even_after_reload():
    a, _, _ = setup()
    a.planner.plan_result = search(c("category", "eq", "ROOM"), c("rent.monthly_amount", "lt", 3500000), city="HCM", ward="Gò Vấp", context={"occupants": 2})
    chat = Conversation(a)
    first = await chat.ask("Tìm phòng trọ phường Gò Vấp, HCM dưới 3,5 triệu cho 2 người")
    assert len(a.tools.value_calls) == 1
    assert rs.sanitize(first.listing_context["searchState"])["boundaryArea"]
    a.planner.plan_result = search(moreResults=True)
    await chat.ask("Xem thêm")
    a.planner.plan_result = search(c("rent.monthly_amount", "lte", 4000000))
    await chat.ask("Tăng ngân sách tối đa 4 triệu")
    assert len(a.tools.value_calls) == 1
    a.planner.plan_result = search(ward="An Nhơn", city="HCM")
    await chat.ask("Chuyển sang phường An Nhơn, HCM")
    assert len(a.tools.value_calls) == 2
    a.planner.plan_result = search(city="Hà Nội")
    await chat.ask("Đổi sang thành phố Hà Nội")
    assert len(a.tools.value_calls) == 3
    state = rs.sanitize(chat.state())
    a.planner.plan_result = search(c("rent.monthly_amount", "lte", 4500000))
    await a.try_answer("Tăng lên 4,5 triệu", chat.messages[-8:], "restored", state=state)
    assert len(a.tools.value_calls) == 3


def test_legacy_paging_and_explicit_reset_in_same_area_cannot_retrigger_boundary():
    state = rs.empty_state() | {"location": {"cityId": "12", "wardId": "24588"}}
    state = rs.record_shown(state, IDS[:4], more=False)
    assert not rs.boundary_turn(state, deepcopy(state), more=False, within=False)
    state["boundaryArea"] = rs.boundary_area(state)
    reset = rs.merge(state, {}, "Tìm lại từ đầu") | {"location": state["location"]}
    assert not rs.boundary_turn(state, reset, more=False, within=False)
