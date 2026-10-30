"""Main paging and boundary advice have independent budgets, IDs and persisted state."""
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.renter_criteria import boundary_policy, grounded_needs
from homespace_ai.application.use_cases.listing_ask import criteria_lines
from homespace_ai.tools.listings import ListingTools, ListingToolError
from tests.unit.listing_fakes import c, make_assistant, search, room, Conversation

MAIN = [f"00000000-0000-0000-0000-{i:012d}" for i in range(1, 44)]
EXTRA = [f"00000000-0000-0000-0000-{i:012d}" for i in (100, 101)]
QUESTION = "Mình muốn thuê phòng trọ ở phường Gò Vấp, TP.HCM, dưới 4,5 triệu/tháng. Cho mình xem 4 phòng mới đăng gần đây."


def make():
    assistant = make_assistant(search(c("category", "eq", "ROOM"), c("rent.monthly_amount", "lt", "4500000"),
                                       ward="Gò Vấp", city="HCM", sort="newest"), MAIN)
    for i, identifier in enumerate(MAIN):
        assistant.tools.details[identifier] = room(identifier, areaM2=[25, 16, 21, 35][i % 4],
            pricing={"amount": [3400000, 2700000, 3100000, 4100000][i % 4], "unit": "ROOM_MONTH", "currency": "VND"},
            roomDetail={"maxOccupants": 2, "restroomType": "PRIVATE", "balconyType": "PRIVATE" if i % 4 in (1, 2) else "NONE"})
    for i, identifier in enumerate(EXTRA):
        assistant.tools.details[identifier] = room(identifier, areaM2=45 + i * 5,
            pricing={"amount": 4800000 + i * 100000, "unit": "ROOM_MONTH", "currency": "VND"},
            roomDetail={"maxOccupants": 2, "restroomType": "PRIVATE", "balconyType": "PRIVATE"})
    assistant.tools.value_boundary = {"candidateIds": EXTRA, "budgetField": "rent.monthly_amount",
                                       "budgetCeiling": 4500000, "maxExtraMonthly": 500000}
    return assistant


def choose(assistant, candidate, main):
    assistant.generative_client.boundary_decision = {
        "selectedId": candidate, "comparedListingIds": main,
        "advantages": [{"field": "areaM2", "againstIds": main}], "tradeoffs": []}


@pytest.mark.asyncio
async def test_43_matches_page_two_has35_remaining_and_independent_new_boundary_choice():
    assistant = make()
    choose(assistant, EXTRA[0], MAIN[:4])
    conversation = Conversation(assistant)
    first = await conversation.ask(QUESTION)
    assert first.status == "ANSWERED" and first.listing_ids == MAIN[:4]
    assert "300.000" in first.answer and EXTRA[0] in first.answer
    assert first.listing_context["listingIds"] == MAIN[:4]
    state = first.listing_context["searchState"]
    assert state["shown"]["ids"] == MAIN[:4]
    assert rs.boundary_exclusions(state) == [EXTRA[0]]
    original_main = deepcopy(assistant.tools.payloads[0])
    choose(assistant, EXTRA[1], MAIN[4:8])
    second = await conversation.ask("Xem thêm")
    assert second.status == "ANSWERED" and second.listing_ids == MAIN[4:8]
    assert "35 tin" in second.answer and EXTRA[1] in second.answer and EXTRA[0] not in second.answer
    assert assistant.tools.payloads[1]["excludeIds"] == MAIN[:4]
    canonical = lambda where: {tuple(sorted(leaf.items())) for leaf in where["all"]}
    assert canonical(assistant.tools.payloads[1]["where"]) == canonical(original_main["where"]), "Main budget never increases to5m"
    assert assistant.tools.value_calls[1][2] == [EXTRA[0]], "Boundary IDs have their own exclusions"
    assert rs.boundary_exclusions(second.listing_context["searchState"]) == EXTRA
    assert second.listing_context["searchState"]["shown"]["ids"] == MAIN[:8]
    assert not assistant.tools.nearby_calls


@pytest.mark.asyncio
async def test_strict_budget_survives_more_turn_and_restart_state_without_model_guessing():
    assistant = make()
    conversation = Conversation(assistant)
    first = await conversation.ask(QUESTION + " Đây là hết khả năng, mình không thể tăng ngân sách.")
    assert first.listing_context["searchState"]["boundaryAdvice"] == "DENY"
    assert not assistant.tools.value_calls
    stored = rs.sanitize(first.listing_context["searchState"])
    second = await assistant.try_answer("Xem thêm", conversation.messages[-2:], "restart", state=stored)
    assert second.listing_context["searchState"]["boundaryAdvice"] == "DENY"
    assert not assistant.tools.value_calls
    assert second.listing_ids == MAIN[4:8]


@pytest.mark.asyncio
async def test_explicit_opt_out_on_detail_shortcut_persists_to_pagination():
    assistant = make()
    conversation = Conversation(assistant)
    await conversation.ask(QUESTION)
    calls = len(assistant.tools.value_calls)
    opted_out = await conversation.ask("Đừng đề xuất phòng vượt ngân sách nữa")
    assert opted_out.listing_context["searchState"]["boundaryAdvice"] == "DENY"
    await conversation.ask("Xem thêm")
    assert len(assistant.tools.value_calls) == calls


@pytest.mark.asyncio
async def test_semantic_abstention_and_timeout_leave_valid_table_and_budget_intact():
    assistant = make()
    first = await assistant.try_answer(QUESTION, [], "no-value")
    assert first.status == "ANSWERED" and first.listing_ids == MAIN[:4]
    assert all(identifier not in first.answer for identifier in EXTRA)
    assert not rs.boundary_exclusions(first.listing_context["searchState"])
    assistant.generative_client.generate_value_advice = AsyncMock(side_effect=TimeoutError())
    second = await assistant.try_answer(QUESTION, [], "judge-timeout")
    assert second.status == "ANSWERED" and second.listing_ids == MAIN[:4]
    assert second.listing_context["searchState"]["constraints"] == first.listing_context["searchState"]["constraints"]


def test_boundary_history_does_not_reset_main_fingerprint_and_preferences_reset_only_advice():
    state = rs.empty_state()
    state["constraints"] = [c("rent.monthly_amount", "lt", "4500000")]
    fingerprint = rs.fingerprint(state)
    remembered = rs.record_boundary(state, EXTRA[0])
    assert rs.fingerprint(remembered) == fingerprint
    remembered["boundaryAdvice"] = "DENY"
    assert rs.fingerprint(remembered) == fingerprint
    assert rs.boundary_exclusions(rs.sanitize(remembered)) == [EXTRA[0]]
    remembered["constraints"].append(c("room.balcony_type", "eq", "PRIVATE", strength="preference"))
    assert rs.fingerprint(remembered) == fingerprint and rs.boundary_exclusions(remembered) == []


def test_generic_recommendation_or_hypothesis_does_not_authorize_spending_more():
    assert boundary_policy("Gợi ý phòng trọ ở An Nhơn", "ALLOW") is None
    assert boundary_policy("Nếu tăng ngân sách thì sao?", "ALLOW") is None
    assert boundary_policy("Mình sẵn sàng tăng ngân sách để cân nhắc phòng tốt hơn", "ALLOW") == "ALLOW"


def test_qualitative_needs_are_customer_quotes_and_survive_pagination_and_restart():
    question = "Bọn mình đã có giường tủ nên ưu tiên phòng rộng, nội thất không quan trọng. Cho mình xem 4 phòng mới đăng."
    notes = grounded_needs(question, ["ưu tiên phòng rộng", "diện tích ít nhất 30m²", "sống cùng gia đình"])
    assert "ưu tiên phòng rộng" in notes and "nội thất không quan trọng" in notes
    assert "diện tích ít nhất 30m²" not in notes and "sống cùng gia đình" not in notes
    state = rs.merge(None, {"needs": notes}, question)
    main_fingerprint, boundary_fingerprint = rs.fingerprint(state), rs.boundary_fingerprint(state)
    state = rs.record_boundary(state, EXTRA[0])
    state = rs.merge(rs.sanitize(state), {"needs": []}, "Xem thêm")
    assert state["needs"] == notes and rs.boundary_exclusions(state) == [EXTRA[0]]
    assert rs.fingerprint(state) == main_fingerprint
    changed = rs.merge(state, {"removeFields": ["needs"], "needs": ["cần đầy đủ nội thất"]}, "Giờ mình cần đầy đủ nội thất")
    assert changed["needs"] == ["cần đầy đủ nội thất"]
    assert rs.fingerprint(changed) == main_fingerprint
    assert rs.boundary_fingerprint(changed) != boundary_fingerprint and rs.boundary_exclusions(changed) == []
    assert rs.sanitize(rs.empty_state() | {"needs": "not a list"})["needs"] == []


@pytest.mark.asyncio
async def test_exact_renter_needs_reach_the_boundary_judge_on_followup():
    assistant = make()
    assistant.planner.plan_result.arguments["valuePriorities"] = [
        {"field": "area_m2", "direction": "HIGHER", "quote": "ưu tiên phòng rộng"},
        {"field": "room.furnishing_status", "direction": "IGNORE", "quote": "nội thất không quan trọng"},
        {"field": "area_m2", "direction": "LOWER", "quote": "mình thích phòng nhỏ"},
        {"field": "invented_field", "direction": "IGNORE", "quote": "ưu tiên phòng rộng"}]
    conversation = Conversation(assistant)
    question = QUESTION + " Bọn mình đã có giường tủ nên ưu tiên phòng rộng, nội thất không quan trọng."
    first = await conversation.ask(question)
    notes = first.listing_context["searchState"]["needs"]
    assert notes and "nội thất không quan trọng" in notes
    assert "| Diện tích |" in first.answer and "| Giường |" not in first.answer
    assert assistant.generative_client.boundary_calls[0]["confirmedCriteria"]["needs"] == notes
    priorities = first.listing_context["searchState"]["valuePriorities"]
    assert len(priorities) == 2 and priorities[0]["direction"] == "HIGHER"
    await conversation.ask("Xem thêm")
    assert assistant.generative_client.boundary_calls[1]["confirmedCriteria"]["needs"] == notes
    assert assistant.generative_client.boundary_calls[1]["confirmedCriteria"]["valuePriorities"] == priorities


@pytest.mark.asyncio
async def test_core_tool_contract_is_one_bounded_read_and_excludes_previous_suggestions():
    gateway = AsyncMock()
    payload = {"version": 2, "limit": 4}
    boundary = {"candidateIds": [EXTRA[1]], "budgetField": "rent.monthly_amount", "budgetCeiling": 4500000,
                "maxExtraMonthly": 500000, "readAt": "now"}
    gateway.request.return_value = httpx.Response(200, json={"result": boundary})
    tool = ListingTools(gateway, None, 20)
    assert await tool.value_opportunity(payload, MAIN[:4], EXTRA[:1]) == boundary
    gateway.request.assert_awaited_once_with("POST", "/api/v1/public/listings/ai/value-opportunity", request=None,
        json={"search": payload, "displayedIds": MAIN[:4], "excludedSuggestionIds": EXTRA[:1]}, timeout=5.0)
    gateway.request.return_value = httpx.Response(200, json={"result": boundary | {"candidateIds": [EXTRA[0]]}})
    with pytest.raises(ListingToolError):
        await tool.value_opportunity(payload, MAIN[:4], EXTRA[:1])


@pytest.mark.asyncio
async def test_search_tool_uses_matched_total_for_page_counts_without_dropping_filters():
    gateway = AsyncMock()
    gateway.request.return_value = httpx.Response(200, json={"result": {"version": 2, "listingIds": MAIN[4:8],
        "total": 39, "matchedTotal": 43, "unknownTotal": 0, "evidence": []}})
    tool = ListingTools(gateway, None, 20)
    result = await tool.search_v2({"version": 2, "limit": 4, "excludeIds": MAIN[:4]})
    assert result["total"] == 43 and result["listingIds"] == MAIN[4:8]
