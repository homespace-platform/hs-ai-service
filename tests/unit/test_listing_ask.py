"""Listing flow v2: criteria state, clarification, hard filters, no silent relaxation (T01–T37)."""
import json

import pytest

from homespace_ai.application.listing_location import resolve_location_filters
from homespace_ai.application.renter_search_state import latest_state
from homespace_ai.clients.generative import GenerationUnavailableError, LISTING_SYSTEM_PROMPT
from homespace_ai.clients.listing_tool_planner import ToolPlan
from homespace_ai.tools.listings import ListingSearchRejected
from tests.unit.listing_fakes import (CATALOG, Conversation, FakeTools, ID, IDS, c, leaves, make_assistant,
                                      room, search)

HANH_THONG = next(item["value"] for item in resolve_location_filters(
    [{"field": "ward", "op": "eq", "value": "Hạnh Thông"}, {"field": "city", "op": "eq", "value": "HCM"}],
    "phường Hạnh Thông HCM", [], {"ward_id": "TEXT", "city_id": "TEXT"}) if item["field"] == "ward_id")
WARD = {"field": "ward_id", "op": "eq", "value": HANH_THONG}
CITY = {"field": "city_id", "op": "eq", "value": "12"}
ELEVATOR = {"field": "amenity.ELEVATOR", "op": "eq", "value": "true"}


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "Quét các tiện ích trong bán kính 3 km giúp mình.",
    "Trong các phòng này, phòng nào gần bệnh viện hơn?",
    "Phòng nào gần nhất với siêu thị?",
])
async def test_map_requests_when_nearby_disabled_preserve_selection_without_tools(question, monkeypatch):
    assistant = make_assistant(search(), nearby_query_enabled=False)
    history = [{"role": "assistant", "content": "Mình đã tìm được một phòng.", "listingContext": {"version": 1, "listingIds": [ID]}}]

    async def unexpected(*args, **kwargs):
        pytest.fail("An unavailable map capability must not call tools or the planner")

    monkeypatch.setattr(assistant.tools, "catalog", unexpected)
    monkeypatch.setattr(assistant.planner, "plan", unexpected)
    result = await assistant.try_answer(question, history, "map-unavailable")
    assert result.status == "ANSWERED" and "chưa có dữ liệu" in result.answer
    assert result.listing_context["listingIds"] == [ID]


@pytest.mark.asyncio
async def test_t01_elevator_only_asks_for_area_and_never_searches():
    assistant = make_assistant(search(c("amenity.ELEVATOR", "eq", "true")))
    result = await assistant.try_answer("Tìm phòng có thang máy", [], "t01")
    assert result.status == "ANSWERED"
    assert "khu vực" in result.answer and "toàn quốc" not in result.answer
    assert assistant.tools.search_calls == 0
    state = result.listing_context["searchState"]
    assert state["pending"]["type"] == "LOCATION"
    assert [(x["field"], x["value"]) for x in state["constraints"]] == [("amenity.ELEVATOR", True)]


@pytest.mark.asyncio
async def test_t02_short_location_reply_merges_with_pending_elevator_need():
    plans = iter([search(c("amenity.ELEVATOR", "eq", "true")), search(ward="Hạnh Thông", city="HCM")])
    assistant = make_assistant(lambda question: next(plans))
    talk = Conversation(assistant)
    await talk.ask("Tìm phòng có thang máy")
    result = await talk.ask("Hạnh Thông, HCM")
    assert assistant.tools.search_calls == 1
    where = leaves(assistant.tools.payloads[0])
    assert ELEVATOR in where and WARD in where and CITY in where
    assert result.listing_ids == [ID]
    assert assistant.planner.state["pending"]["type"] == "LOCATION"


@pytest.mark.asyncio
async def test_t03_complete_request_sends_every_hard_filter_and_no_keyword_substitute():
    assistant = make_assistant(search(c("amenity.ELEVATOR", "eq", "true"), c("rent.monthly_amount", "lte", "5000000"),
                                      ward="Hạnh Thông", city="HCM", keyword="thang máy"))
    await assistant.try_answer("Tìm phòng ở Hạnh Thông HCM cho 3 người, tối đa 5 triệu, có thang máy", [], "t03")
    payload = assistant.tools.payloads[0]
    where = leaves(payload)
    for expected in [ELEVATOR, WARD, CITY, {"field": "rent.monthly_amount", "op": "lte", "value": "5000000"},
                     {"field": "occupancy.max_occupants", "op": "gte", "value": 3}]:
        assert expected in where
    assert payload["context"] == {"occupants": 3} and payload["version"] == 2
    assert not any(item.get("field") == "category" for item in where), "'phòng' alone is not ROOM"


@pytest.mark.asyncio
async def test_search_logs_are_correlated_and_measured_without_customer_text():
    import structlog
    assistant = make_assistant(search(c("amenity.ELEVATOR", "eq", "true"), ward="Hạnh Thông", city="HCM"))
    question = "Tìm phòng ở Hạnh Thông HCM có thang máy, số mình 0909123456"
    with structlog.testing.capture_logs() as logs:
        await assistant.try_answer(question, [], "req-log")
    result = next(entry for entry in logs if entry["event"] == "listing_search_result")
    assert result["request_id"] == "req-log" and result["filters"] == 3 and result["returned"] == 1
    assert result["payload_bytes"] > 0 and result["latency_ms"] >= 0
    assert all("0909123456" not in repr(entry) and question not in repr(entry) for entry in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("question,op", [("Tìm phòng ở Hạnh Thông HCM dưới 5 triệu", "lt"),
                                         ("Tìm phòng ở Hạnh Thông HCM tối đa 5tr", "lte"),
                                         ("Tìm phòng ở Hạnh Thông HCM không quá 5 củ", "lte"),
                                         ("Tìm phòng ở Hạnh Thông HCM ít nhất 5 triệu", "gte")])
async def test_t04_budget_boundaries_follow_the_words_not_the_model(question, op):
    assistant = make_assistant(search(c("price_amount", "lte", "5000000"), ward="Hạnh Thông", city="HCM"))
    await assistant.try_answer(question, [], "t04")
    assert {"field": "rent.monthly_amount", "op": op, "value": "5000000"} in leaves(assistant.tools.payloads[0])


@pytest.mark.asyncio
async def test_t05_empty_hard_search_is_reported_once_never_relaxed():
    assistant = make_assistant(search(c("room.restroom_type", "eq", "PRIVATE"), c("amenity.ELEVATOR", "eq", "true"),
                                      ward="Hạnh Thông", city="HCM"), ids=[])
    result = await assistant.try_answer("Tìm phòng trọ WC riêng có thang máy ở Hạnh Thông HCM", [], "t05")
    assert result.status == "NO_RESULTS"
    assert assistant.tools.search_calls == 1, "No silent retry that drops a hard filter"
    assert "không tự nới" in result.answer and "Thang máy" in result.answer
    assert not result.listing_ids and assistant.generative_client.kwargs is None
    assert result.listing_context["searchState"]["pending"]["type"] == "EMPTY_RESULT"


@pytest.mark.asyncio
async def test_t06_removing_elevator_keeps_budget_people_and_area():
    plans = iter([search(c("amenity.ELEVATOR", "eq", "true"), c("rent.monthly_amount", "lte", "5000000"),
                         ward="Hạnh Thông", city="HCM"),
                  search(removeFields=["amenity.ELEVATOR"])])
    assistant = make_assistant(lambda question: next(plans))
    talk = Conversation(assistant)
    await talk.ask("Tìm phòng cho 3 người ở Hạnh Thông HCM tối đa 5 triệu có thang máy")
    await talk.ask("Bỏ yêu cầu thang máy đi")
    where = leaves(assistant.tools.payloads[1])
    assert ELEVATOR not in where
    assert WARD in where and {"field": "rent.monthly_amount", "op": "lte", "value": "5000000"} in where
    assert {"field": "occupancy.max_occupants", "op": "gte", "value": 3} in where


@pytest.mark.asyncio
async def test_no_need_for_elevator_is_a_removal_not_a_negative_filter():
    plans = iter([search(c("amenity.ELEVATOR", "eq", "true"), ward="Hạnh Thông", city="HCM"),
                  search(c("amenity.ELEVATOR", "eq", "false"))])
    assistant = make_assistant(lambda question: next(plans))
    talk = Conversation(assistant)
    await talk.ask("Tìm phòng có thang máy ở Hạnh Thông HCM")
    await talk.ask("Không cần thang máy nữa")
    assert not any(item.get("field") == "amenity.ELEVATOR" for item in leaves(assistant.tools.payloads[1]))


@pytest.mark.asyncio
async def test_t07_state_survives_more_than_eight_messages_and_only_budget_changes():
    plans = [search(c("amenity.ELEVATOR", "eq", "true"), c("rent.monthly_amount", "lte", "3000000"),
                    c("room.restroom_type", "eq", "PRIVATE"), ward="Hạnh Thông", city="HCM")]
    plans += [search(c("rent.monthly_amount", "lte", "5000000"))]
    queue = iter(plans)
    assistant = make_assistant(lambda question: next(queue))
    talk = Conversation(assistant)
    await talk.ask("Tìm phòng trọ WC riêng có thang máy ở Hạnh Thông HCM tối đa 3 triệu")
    for turn in range(5):
        await talk.ask(f"Tin vừa rồi có gì đặc biệt (lượt {turn})?")
    assert len(talk.messages) > 8 and assistant.planner.calls == 1, "Detail reads skip the planner"
    await talk.ask("Tăng ngân sách lên tối đa 5 triệu")
    where = leaves(assistant.tools.payloads[-1])
    assert {"field": "rent.monthly_amount", "op": "lte", "value": "5000000"} in where
    assert {"field": "rent.monthly_amount", "op": "lte", "value": "3000000"} not in where
    for expected in [ELEVATOR, WARD, CITY, {"field": "room.restroom_type", "op": "eq", "value": "PRIVATE"},
                     {"field": "category", "op": "eq", "value": "ROOM"}]:
        assert expected in where


@pytest.mark.asyncio
async def test_t08_legacy_listing_ids_only_conversation_still_reads_references_without_inventing_criteria():
    assistant = make_assistant(None)
    history = [{"role": "assistant", "content": "Phòng cũ", "listingContext": {"version": 1, "listingIds": [ID]}}]
    assert latest_state(history) is None
    result = await assistant.try_answer("Phòng 1 có thang máy không?", history, "legacy")
    assert result.status == "ANSWERED" and assistant.tools.read == [ID]
    assert assistant.tools.search_calls == 0


@pytest.mark.asyncio
async def test_t09_named_landmark_uses_target_coordinates_not_title_keyword():
    target = {"placeId": "11111111-1111-1111-1111-111111111111", "name": "Đại Học Công Nghiệp", "address": "12 Nguyễn Văn Bảo"}
    assistant = make_assistant(search(c("rent.monthly_amount", "lt", "4000000"), place="IUH", keyword="IUH"))
    assistant.tools.places = {"IUH": [target]}
    result = await assistant.try_answer("Tìm phòng gần IUH dưới 4 triệu", [], "t09")
    payload = assistant.tools.payloads[0]
    assert payload["nearbyAll"] == [{"placeId": target["placeId"], "radiusMeters": 2000}]
    assert "keyword" not in payload
    assert result.listing_ids == [ID]
    assert result.listing_context["searchState"]["target"]["placeId"] == target["placeId"]


@pytest.mark.asyncio
async def test_t10_two_campuses_ask_then_short_choice_keeps_budget_and_people():
    first = {"placeId": "11111111-1111-1111-1111-111111111111", "name": "Đại Học Công Nghiệp", "address": "12 Nguyễn Văn Bảo"}
    second = {"placeId": "22222222-2222-2222-2222-222222222222", "name": "Trường Đại Học Công Nghiệp", "address": "12 Nguyễn Văn Nghi"}
    assistant = make_assistant(search(c("rent.monthly_amount", "lte", "4000000"), place="IUH"))
    assistant.tools.places = {"IUH": [first, second]}
    talk = Conversation(assistant)
    asked = await talk.ask("Tìm phòng cho 2 người gần IUH tối đa 4 triệu")
    assert "cơ sở nào" in asked.answer and "Nguyễn Văn Nghi" in asked.answer
    assert assistant.tools.search_calls == 0
    await talk.ask("cơ sở 2")
    assert assistant.planner.calls == 1, "The choice is resolved from Core candidates, not re-planned"
    payload = assistant.tools.payloads[0]
    assert payload["nearbyAll"][0]["placeId"] == second["placeId"]
    assert {"field": "rent.monthly_amount", "op": "lte", "value": "4000000"} in leaves(payload)
    assert payload["context"]["occupants"] == 2


@pytest.mark.asyncio
async def test_unknown_place_asks_without_switching_to_any_school():
    assistant = make_assistant(search(place="ĐH Không Tồn Tại"))
    result = await assistant.try_answer("Tìm phòng gần ĐH Không Tồn Tại", [], "no-place")
    assert "chưa tìm thấy" in result.answer and assistant.tools.search_calls == 0


@pytest.mark.asyncio
async def test_t13_target_and_supermarket_are_two_independent_predicates():
    target = {"placeId": "11111111-1111-1111-1111-111111111111", "name": "IUH", "address": ""}
    assistant = make_assistant(search(place="IUH", radius=2000, nearby=[{"kind": "SUPERMARKET", "group": None, "radiusMeters": 500}]))
    assistant.tools.places = {"IUH": [target]}
    await assistant.try_answer("Phòng gần IUH trong 2 km và có siêu thị trong 500 m", [], "t13")
    assert assistant.tools.payloads[0]["nearbyAll"] == [{"placeId": target["placeId"], "radiusMeters": 2000},
                                                         {"kind": "SUPERMARKET", "radiusMeters": 500}]


@pytest.mark.asyncio
async def test_t16_room_or_apartment_for_three_is_two_categories_with_capacity():
    assistant = make_assistant(search(c("category", "eq", "APARTMENT"), ward="Hạnh Thông", city="HCM"))
    await assistant.try_answer("Tìm phòng trọ hoặc căn hộ ở 3 người tại Hạnh Thông HCM", [], "t16")
    where = leaves(assistant.tools.payloads[0])
    assert {"field": "category", "op": "in", "value": ["APARTMENT", "ROOM"]} in where
    assert {"field": "occupancy.max_occupants", "op": "gte", "value": 3} in where


@pytest.mark.asyncio
async def test_t22_total_monthly_cost_uses_computed_field_and_renter_context():
    assistant = make_assistant(search(c("cost.fixed_monthly_total", "lte", "5000000"), ward="Hạnh Thông", city="HCM"))
    await assistant.try_answer("Tìm phòng ở Hạnh Thông HCM tổng chi phí tối đa 5 triệu cho 3 người, 2 xe máy", [], "t22")
    payload = assistant.tools.payloads[0]
    assert {"field": "cost.fixed_monthly_total", "op": "lte", "value": "5000000"} in leaves(payload)
    assert payload["context"] == {"occupants": 3, "motorbikes": 2}


@pytest.mark.asyncio
async def test_t23_relative_move_in_date_and_lease_use_the_request_clock():
    assistant = make_assistant(search(c("available_from", "lte", "2025-01-01"), ward="Hạnh Thông", city="HCM"))
    await assistant.try_answer("Tìm phòng ở Hạnh Thông HCM vào đầu tháng sau, thuê 3 tháng", [], "t23")
    where = leaves(assistant.tools.payloads[0])
    assert {"field": "available_from", "op": "lte", "value": "2026-11-01"} in where
    assert {"field": "minimum_lease_months", "op": "lte", "value": 3} in where


@pytest.mark.asyncio
async def test_t26_t27_more_results_exclude_shown_and_changed_criteria_restart_paging():
    plans = iter([search(ward="Hạnh Thông", city="HCM", limit=3), search(moreResults=True, limit=3),
                  search(c("amenity.ELEVATOR", "eq", "true"), limit=3)])
    assistant = make_assistant(lambda question: next(plans), ids=IDS)
    talk = Conversation(assistant)
    first = await talk.ask("Tìm 3 phòng ở Hạnh Thông HCM")
    second = await talk.ask("Cho 3 tin khác")
    assert first.listing_ids == IDS[:3] and second.listing_ids == IDS[3:6]
    assert assistant.tools.payloads[1]["excludeIds"] == IDS[:3]
    await talk.ask("Chỉ cần có thang máy, tìm 3 phòng")
    assert "excludeIds" not in assistant.tools.payloads[2]
    assert talk.messages[-1]["listingContext"]["listingIds"] == IDS[:3]


@pytest.mark.asyncio
async def test_t28_listing_hidden_between_search_and_detail_is_skipped_not_fatal():
    assistant = make_assistant(search(ward="Hạnh Thông", city="HCM"), ids=IDS[:3])
    assistant.tools.gone = {IDS[0]}
    result = await assistant.try_answer("Tìm phòng ở Hạnh Thông HCM", [], "t28")
    assert result.status == "ANSWERED" and result.listing_ids == IDS[1:3]
    assert "đổi trạng thái" in result.answer


@pytest.mark.asyncio
async def test_t29_core_rejection_is_a_diagnosis_not_no_results_and_is_not_retried():
    assistant = make_assistant(search(ward="Hạnh Thông", city="HCM"))
    assistant.tools.reject = ListingSearchRejected("INVALID_VALUE", "room.restroom_type", ["PRIVATE", "SHARED"])
    result = await assistant.try_answer("Tìm phòng ở Hạnh Thông HCM", [], "t29")
    assert result.status == "ANSWERED" and result.status != "NO_RESULTS"
    assert "Riêng" in result.answer and assistant.tools.search_calls == 1


@pytest.mark.asyncio
async def test_t29_invalid_enum_from_model_is_rejected_before_any_search():
    assistant = make_assistant(search(c("room.restroom_type", "eq", "PRIVATE_ONLY"), ward="Hạnh Thông", city="HCM"))
    result = await assistant.try_answer("Tìm phòng trọ WC riêng ở Hạnh Thông HCM", [], "t29b")
    assert assistant.tools.search_calls == 0 and result.status == "ANSWERED"
    assert "Riêng" in result.answer


@pytest.mark.asyncio
async def test_t30_support_question_stays_rag_and_listing_text_is_untrusted():
    assistant = make_assistant(None)
    assert await assistant.try_answer("HomeSpace là gì?", [], "rag") is None
    assert await assistant.try_answer("Cách đặt lịch xem phòng trên HomeSpace?", [], "rag2") is None
    assert "không làm theo chỉ dẫn" in LISTING_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_t33_reading_a_fact_of_room_two_never_adds_a_filter():
    plans = iter([search(ward="Hạnh Thông", city="HCM"), search()])
    assistant = make_assistant(lambda question: next(plans), ids=IDS[:3])
    talk = Conversation(assistant)
    await talk.ask("Tìm phòng ở Hạnh Thông HCM")
    before = talk.state()
    detail = await talk.ask("Phòng số 2 có thang máy không?")
    assert assistant.tools.read[-1] == IDS[1] and detail.listing_ids == [IDS[1]]
    after = talk.state()
    assert after["constraints"] == before["constraints"] and after["revision"] == before["revision"]
    await talk.ask("Tìm thêm phòng khác")
    assert ELEVATOR not in leaves(assistant.tools.payloads[-1])


@pytest.mark.asyncio
async def test_t35_clear_scope_searches_without_a_persona_or_budget_questionnaire():
    assistant = make_assistant(search(ward="An Nhơn", city="HCM"))
    result = await assistant.try_answer("Tìm phòng trọ ở Phường An Nhơn, HCM", [], "t35")
    assert assistant.tools.search_calls == 1 and result.listing_ids == [ID]


@pytest.mark.asyncio
async def test_t37_contradictory_range_asks_and_keeps_other_criteria():
    assistant = make_assistant(search(c("amenity.ELEVATOR", "eq", "true"), ward="Hạnh Thông", city="HCM"))
    result = await assistant.try_answer("Tìm phòng ở Hạnh Thông HCM từ 5 đến 4 triệu có thang máy", [], "t37")
    assert assistant.tools.search_calls == 0
    assert "mâu thuẫn" in result.answer and "chốt" in result.answer
    state = result.listing_context["searchState"]
    assert state["pending"]["type"] == "CONFLICT"
    assert {(x["field"], x["op"]) for x in state["constraints"]} >= {("amenity.ELEVATOR", "eq"), ("rent.monthly_amount", "gte"), ("rent.monthly_amount", "lte")}


@pytest.mark.asyncio
async def test_approximate_budget_is_a_preference_never_a_hidden_twenty_percent_range():
    assistant = make_assistant(search(c("price_amount", "gte", "2400000"), c("price_amount", "lte", "3600000"),
                                      ward="Hạnh Thông", city="HCM"))
    result = await assistant.try_answer("Tìm phòng ở Hạnh Thông HCM tầm 3 triệu", [], "approx")
    assert not any(item.get("field") in {"rent.monthly_amount", "price_amount"} for item in leaves(assistant.tools.payloads[0]))
    kept = result.listing_context["searchState"]["constraints"]
    assert [(x["field"], x["op"], x["strength"]) for x in kept] == [("rent.monthly_amount", "approx", "preference")]


@pytest.mark.asyncio
async def test_self_description_is_not_a_house_rule_filter():
    assistant = make_assistant(search(c("policy.smokingPolicy.value", "eq", "NOT_ALLOWED"), ward="Hạnh Thông", city="HCM"))
    await assistant.try_answer("Mình không hút thuốc, tìm phòng ở Hạnh Thông HCM", [], "self")
    assert not any(str(item.get("field", "")).startswith("policy.") for item in leaves(assistant.tools.payloads[0]))


@pytest.mark.asyncio
async def test_category_change_asks_about_incompatible_room_fields_then_applies_confirmed_change():
    plans = iter([search(c("room.balcony_type", "eq", "PRIVATE"), ward="Hạnh Thông", city="HCM"), search()])
    assistant = make_assistant(lambda question: next(plans))
    talk = Conversation(assistant)
    await talk.ask("Tìm phòng trọ ban công riêng ở Hạnh Thông HCM")
    asked = await talk.ask("Đổi sang căn hộ")
    assert "không áp dụng" in asked.answer and assistant.tools.search_calls == 1
    await talk.ask("Đồng ý")
    where = leaves(assistant.tools.payloads[-1])
    assert {"field": "category", "op": "eq", "value": "APARTMENT"} in where
    assert not any(str(item.get("field", "")).startswith("room.") for item in where)
    assert WARD in where


@pytest.mark.asyncio
async def test_hypothetical_extra_person_is_answered_but_not_persisted():
    plans = iter([search(ward="Hạnh Thông", city="HCM"), search(withinDisplayed=True)])
    assistant = make_assistant(lambda question: next(plans), ids=IDS[:3])
    talk = Conversation(assistant)
    await talk.ask("Tìm phòng cho 2 người ở Hạnh Thông HCM")
    await talk.ask("Nếu thêm 1 người thành 3 người thì sao?")
    assert assistant.tools.payloads[-1]["context"]["occupants"] == 3
    assert talk.state()["context"]["occupants"] == 2


@pytest.mark.asyncio
async def test_nationwide_is_not_a_chat_search_scope():
    """Web/Core have no nationwide search; chat always asks for a real place."""
    for question, plan in [
        ("Tìm phòng trọ có thang máy toàn quốc", search(c("amenity.ELEVATOR", "eq", "true"))),
        ("Tìm phòng trọ có thang máy", search(c("amenity.ELEVATOR", "eq", "true"), nationwide=True)),
        ("Cứ tìm trên toàn quốc giúp mình", search(nationwide=True)),
    ]:
        assistant = make_assistant(plan)
        result = await assistant.try_answer(question, [], "nationwide")
        assert assistant.tools.search_calls == 0 and "khu vực" in result.answer
        assert "toàn quốc" not in result.answer


@pytest.mark.asyncio
async def test_latest_listings_without_area_ask_first():
    assistant = make_assistant(search(sort="newest", limit=10))
    result = await assistant.try_answer("Tìm 10 tin đăng mới gần nhất", [], "newest")
    assert assistant.tools.search_calls == 0 and "khu vực" in result.answer


@pytest.mark.asyncio
async def test_model_question_without_tool_call_is_kept_as_validated_clarification():
    plan = ToolPlan("ask_clarification", {"question": "Bạn muốn ở gần cơ sở Nguyễn Văn Bảo hay Nguyễn Văn Nghi?",
                                          "missing": ["TARGET"], "options": []})
    assistant = make_assistant(plan)
    result = await assistant.try_answer("Tìm phòng gần trường mình học", [], "clarify")
    assert "Nguyễn Văn Bảo hay Nguyễn Văn Nghi" in result.answer and assistant.tools.search_calls == 0
    assert result.listing_context["searchState"]["pending"]["type"] == "TARGET"


@pytest.mark.asyncio
async def test_listing_question_uses_core_data_and_parking_template():
    assistant = make_assistant(ToolPlan("get_listing_detail", {"listingId": ID}))
    history = [{"role": "assistant", "content": "Mình vừa tìm được một phòng.", "listingContext": {"version": 1, "listingIds": [ID]}}]
    result = await assistant.try_answer("Phòng đó gửi xe có phí không?", history, "req-1")
    assert result.status == "ANSWERED" and result.listing_ids == [ID]
    assert "có thu phí" in result.answer and "80.000" in result.answer
    assert assistant.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_browse_answer_lists_criteria_and_only_matching_category_cards():
    house = "00000000-0000-0000-0000-000000000002"
    assistant = make_assistant(search(ward="Hạnh Thông", city="HCM"), ids=[ID, house])
    assistant.tools.details = {house: room(house, category="HOUSE")}
    result = await assistant.try_answer("Tìm phòng trọ ở Hạnh Thông HCM", [], "browse")
    assert result.listing_ids == [ID]
    assert "Tiêu chí đang áp dụng" in result.answer
    assert result.answer.count("/rent/") == 1 and f"](/rent/{ID}" in result.answer, "Only the table header links; no image"
    assert assistant.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_general_room_comparison_is_compact_markdown_table():
    ids = IDS[:4]
    assistant = make_assistant(ToolPlan("get_listing_details", {"listingIds": ids}), ids=ids)
    history = [{"role": "assistant", "content": "4 phòng", "listingContext": {"version": 1, "listingIds": ids}}]
    result = await assistant.try_answer("Tôi muốn so sánh 4 phòng trên", history, "compare")
    assert "| Tiêu chí |" in result.answer and result.listing_ids == ids
    assert assistant.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_generation_failure_after_tool_read_does_not_claim_tool_failed():
    assistant = make_assistant(ToolPlan("get_listing_detail", {"listingId": ID}))

    class FailedGenerator:
        async def generate_listing_answer(self, **kwargs):
            raise GenerationUnavailableError("answer was cut off")

    assistant.generative_client = FailedGenerator()
    history = [{"role": "assistant", "content": "1 phòng", "listingContext": {"version": 1, "listingIds": [ID]}}]
    result = await assistant.try_answer("Vì sao phòng này có tiện ích tòa nhà phù hợp?", history, "truncated")
    assert result.status == "GENERATION_UNAVAILABLE" and "đã kiểm tra tin đăng" in result.answer


@pytest.mark.asyncio
async def test_evidence_contains_server_built_checks_and_requested_criteria():
    assistant = make_assistant(search(c("amenity.ELEVATOR", "eq", "true"), ward="Hạnh Thông", city="HCM"))
    assistant.tools.evidence = [{"listingId": ID, "targetDistanceMeters": None,
                                 "checks": [{"fields": ["amenity.ELEVATOR"], "status": "MATCH", "actual": None}]}]
    await assistant.try_answer("Có phòng nào ở Hạnh Thông HCM có thang máy, nên chọn phòng nào?", [], "evidence")
    evidence = assistant.generative_client.evidence
    assert any("Thang máy" in line for line in evidence["requestedCriteria"])
    assert evidence["listings"][0]["criteriaChecks"][0]["status"] == "MATCH"
    assert "relaxedFilters" not in json.dumps(evidence)


def test_fixture_is_the_core_v2_catalog():
    assert CATALOG["version"] == 2 and CATALOG["catalogVersion"].startswith("fields-v2-")
    codes = {f["code"] for f in CATALOG["fields"]}
    for needed in ["amenity.ELEVATOR", "room.restroom_type", "room.access_hours_type", "room.balcony_type",
                   "occupancy.max_occupants", "rent.monthly_amount", "cost.fixed_monthly_total",
                   "policy.propertyInspectionNotice.value", "policy.noticeDaysBeforeTermination.value",
                   "policy.depositRefundDays.value", "furnishing.RANGE_HOOD.total_quantity", "furnishing.MICROWAVE",
                   "furnishing.SHOE_CABINET.handover_condition", "furnishing.WATER_TANK_PUMP.quantity"]:
        assert needed in codes
    pet = next(f for f in CATALOG["fields"] if f["code"] == "policy.petPolicy.value")
    assert [v["code"] for v in pet["values"]] == ["ALLOWED", "NOT_ALLOWED", "APPROVAL_REQUIRED"]
