"""Optional budget advice reports evidence outcomes without inventing a recommendation."""
from unittest.mock import AsyncMock

import pytest

from homespace_ai.application import listing_value_advice as value
from homespace_ai.tools.listings import ListingToolError
from tests.unit.test_boundary_pagination import make, choose, QUESTION, MAIN, EXTRA
from tests.unit.listing_fakes import Conversation, search, c

NO_OFFER = "Ngoài ra, mình chưa thấy phòng nào đáng để bạn tăng thêm ngân sách."


@pytest.mark.asyncio
async def test_empty_verified_core_pool_explains_absence_and_keeps_main_results():
    a = make()
    a.tools.value_boundary["candidateIds"] = []
    result = await a.try_answer(QUESTION, [], "empty-pool")
    check = result.listing_context["boundaryCheck"]
    assert check == {"status": "NO_CANDIDATES", "reason": "EMPTY_CANDIDATE_POOL",
                     "candidateCount": 0, "readCount": 0, "maxExtraMonthly": 500000}
    assert NO_OFFER in result.answer
    assert result.listing_ids == MAIN[:4]
    assert result.status == "ANSWERED" and len(a.tools.value_calls) == 1
    assert "valueSuggestion" not in result.listing_context


@pytest.mark.asyncio
async def test_candidates_without_a_worthwhile_offer_do_not_claim_no_data():
    a = make()
    result = await a.try_answer(QUESTION, [], "abstention")
    assert result.listing_context["boundaryCheck"]["status"] == "NO_CLEAR_ADVANTAGE"
    assert result.listing_context["boundaryCheck"]["reason"] == "MODEL_ABSTAINED"
    assert NO_OFFER in result.answer
    assert "chưa tìm thấy phòng đáp ứng" not in result.answer
    assert "valueSuggestion" not in result.listing_context


@pytest.mark.asyncio
async def test_unverified_proof_does_not_force_suggestion_or_claim_pool_was_empty():
    a = make()
    choose(a, EXTRA[0], MAIN[:4])
    a.generative_client.boundary_decision["selectedId"] = MAIN[0]
    result = await a.try_answer(QUESTION, [], "invalid-proof")
    assert result.listing_context["boundaryCheck"]["reason"] == "INVALID_BOUNDARY_EVIDENCE"
    assert "chưa kiểm tra được đầy đủ" in result.answer
    assert NO_OFFER not in result.answer
    assert "chưa tìm thấy phòng đáp ứng" not in result.answer
    assert result.listing_ids == MAIN[:4] and "valueSuggestion" not in result.listing_context


@pytest.mark.asyncio
async def test_an_advantage_already_available_in_the_group_is_not_recommended():
    a = make()
    choose(a, EXTRA[0], MAIN[:4])
    a.generative_client.boundary_decision["advantages"][0]["field"] = "roomDetail.restroomType"
    result = await a.try_answer(QUESTION, [], "not-a-group-improvement")
    assert result.listing_context["boundaryCheck"]["reason"] == "NO_VERIFIED_WHOLE_PAGE_ADVANTAGE"
    assert NO_OFFER in result.answer
    assert "valueSuggestion" not in result.listing_context


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["tool", "judge"])
async def test_outages_are_not_misrepresented_as_no_matching_rooms(stage):
    a = make()
    if stage == "tool":
        a.tools.value_opportunity = AsyncMock(side_effect=ListingToolError("unavailable"))
    else:
        a.generative_client.generate_value_advice = AsyncMock(side_effect=TimeoutError())
    result = await a.try_answer(QUESTION, [], "outage")
    assert result.listing_context["boundaryCheck"]["status"] == "UNAVAILABLE"
    assert "chưa kiểm tra được đầy đủ" in result.answer and result.status == "ANSWERED"
    assert NO_OFFER not in result.answer
    assert "chưa tìm thấy phòng đáp ứng" not in result.answer
    assert result.listing_ids == MAIN[:4]


@pytest.mark.asyncio
async def test_empty_data_outcome_survives_an_unrelated_main_judge_timeout():
    a = make()
    a.tools.value_boundary["candidateIds"] = []
    a.generative_client.generate_value_advice = AsyncMock(side_effect=TimeoutError())
    result = await a.try_answer(QUESTION, [], "empty-data-judge-timeout")
    assert result.listing_context["boundaryCheck"]["status"] == "NO_CANDIDATES"
    assert NO_OFFER in result.answer


@pytest.mark.asyncio
async def test_success_has_only_the_verified_offer_without_absence_message():
    a = make()
    choose(a, EXTRA[0], MAIN[:4])
    result = await a.try_answer(QUESTION, [], "suggestion")
    assert result.listing_context["boundaryCheck"]["status"] == "SUGGESTED"
    assert result.listing_context["valueSuggestion"]["listingId"] == EXTRA[0]
    assert "ngoài ngân sách để cân nhắc" in result.answer
    assert NO_OFFER not in result.answer
    assert "chưa tìm thấy" not in result.answer and "chưa kiểm tra" not in result.answer


@pytest.mark.asyncio
async def test_same_area_and_pagination_preserve_no_repeat_policy_without_false_search_claim():
    a = make()
    chat = Conversation(a)
    await chat.ask(QUESTION)
    result = await chat.ask("Xem thêm")
    assert result.listing_context["boundaryCheck"] == {"status": "SKIPPED", "reason": "PAGINATION"}
    assert len(a.tools.value_calls) == 1 and NO_OFFER not in result.answer
    result = await chat.ask(QUESTION)
    assert result.listing_context["boundaryCheck"] == {"status": "SKIPPED", "reason": "SAME_AREA"}
    assert len(a.tools.value_calls) == 1 and NO_OFFER not in result.answer
    a.planner.plan_result = search(c("category", "eq", "ROOM"), c("rent.monthly_amount", "lt", "4500000"),
                                  city="HCM", ward="An Nhơn")
    result = await chat.ask("Chuyển sang phường An Nhơn, HCM, dưới 4,5 triệu")
    assert len(a.tools.value_calls) == 2 and result.listing_context["boundaryCheck"]["status"] != "SKIPPED"


@pytest.mark.asyncio
async def test_absolute_budget_never_triggers_an_extra_search_or_absence_message():
    a = make()
    result = await a.try_answer(QUESTION + " Không thể tăng ngân sách.", [], "deny")
    assert result.listing_context["boundaryCheck"] == {"status": "SKIPPED", "reason": "INELIGIBLE"}
    assert not a.tools.value_calls and not value.render_no_boundary(result.listing_context["boundaryCheck"])


@pytest.mark.asyncio
async def test_empty_pool_without_comparable_budget_is_not_an_exhaustive_no_result():
    a = make()
    a.tools.value_boundary.update(candidateIds=[], budgetField=None, budgetCeiling=None)
    result = await a.try_answer(QUESTION, [], "unverified-budget")
    assert result.listing_context["boundaryCheck"]["reason"] == "BUDGET_NOT_COMPARABLE"
    assert "chưa kiểm tra được đầy đủ" in result.answer
    assert NO_OFFER not in result.answer
    assert "chưa tìm thấy phòng đáp ứng" not in result.answer
