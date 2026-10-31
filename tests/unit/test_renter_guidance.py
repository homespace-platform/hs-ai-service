from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homespace_ai.api.v1.agent_ask import public_answer
from homespace_ai.api.v1.schemas import ConversationMessage
from homespace_ai.application import listing_value_advice as value, renter_guidance as guidance, renter_search_state as rs
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.repositories.conversation_repo import ConversationRepository
from homespace_ai.tools.listings import ListingToolError
from tests.unit.listing_fakes import IDS, c, make_assistant, search, room, Conversation
from homespace_ai.clients.listing_tool_planner import ToolPlan


def assistant():
    a = make_assistant(search(c("category", "eq", "ROOM")), IDS[:8])
    a._require_search_basics = True
    for n, identifier in enumerate(IDS[:8]):
        a.tools.details[identifier] = room(identifier, areaM2=26-n,
            pricing={"amount": 3000000, "currency": "VND", "unit": "ROOM_MONTH"})
    a.generative_client.boundary_decision["mainRecommendation"] = {"listingId": IDS[0], "fields": ["areaM2", "pricing.amount"]}
    return a


def complete_plan(**changes):
    return search(c("category", "eq", "ROOM"), c("rent.monthly_amount", "lt", 3500000),
                  city="HCM", ward="Gò Vấp", context={"occupants": 2}, **changes)


@pytest.mark.asyncio
async def test_model_clarification_also_preserves_already_stated_profile_and_search_basics():
    a = assistant()
    a.planner.plan_result = ToolPlan("ask_clarification", {"missing": ["LOCATION"], "question": "Bạn muốn tìm ở đâu?", "options": ["Quận 1"]})
    chat = Conversation(a)
    first = await chat.ask("Mình là sinh viên muốn thuê phòng trọ")
    state = first.listing_context["searchState"]
    assert rs.categories(state) == {"ROOM"} and state["renterProfile"] == {"persona": "STUDENT"}
    assert not a.tools.payloads and "Quận 1" not in first.answer
    a.planner.plan_result = search(c("rent.monthly_amount", "lt", 3500000), city="HCM", ward="Gò Vấp", context={"occupants": 2})
    second = await chat.ask("Phường Gò Vấp, HCM, dưới 3,5 triệu, ở 2 người")
    assert second.status == "ANSWERED" and second.listing_ids == IDS[:4]
    assert second.listing_context["searchState"]["renterProfile"]["persona"] == "STUDENT"


@pytest.mark.asyncio
async def test_question_hint_gives_help_without_a_model_call_or_repeating_the_same_intake():
    a = assistant()
    result = await a.try_answer("Tôi chưa biết nên chọn khu vực nào, bạn tư vấn giúp được không?", [], "help")
    assert "thường xuyên cần đến" in result.answer
    assert a.planner.calls == 0 and not a.tools.payloads


@pytest.mark.asyncio
async def test_intake_keeps_known_criteria_then_searches_and_remembers_student_without_guessed_filters():
    a = assistant()
    chat = Conversation(a)
    first = await chat.ask("Mình là sinh viên, muốn tìm phòng trọ")
    state = first.listing_context["searchState"]
    assert first.status == "NEEDS_CLARIFICATION" and not a.tools.payloads
    assert state["renterProfile"] == {"persona": "STUDENT"} and rs.categories(state) == {"ROOM"}
    assert state["pending"]["fields"] == ["LOCATION", "BUDGET"]
    assert first.suggested_questions and all("[" not in q and "..." not in q for q in first.suggested_questions)
    a.planner.plan_result = complete_plan()
    second = await chat.ask("Gò Vấp, HCM, dưới 3,5 triệu cho 2 người nhé")
    assert second.status == "ANSWERED" and len(a.tools.payloads) == 1 and second.listing_ids == IDS[:4]
    assert second.listing_context["mainRecommendation"] == {"listingId": IDS[0], "fields": ["areaM2"]}
    assert "Mình gợi ý bạn cân nhắc" in second.answer and "Bạn đang học trường nào" in second.answer
    assert len(a.generative_client.boundary_calls) == 1 and not a.tools.nearby_calls
    assert any("Vì sao" in q for q in second.suggested_questions)
    assert second.listing_context["searchState"]["renterProfile"]["persona"] == "STUDENT"
    a.planner.plan_result = search(moreResults=True)
    a.generative_client.boundary_decision["mainRecommendation"] = {"listingId": IDS[4], "fields": ["areaM2"]}
    third = await chat.ask("Xem thêm")
    assert third.listing_ids == IDS[4:8]
    assert "Bạn đang học trường nào" not in third.answer
    assert third.listing_context["searchState"]["context"]["occupants"] == 2
    assert third.listing_context["searchState"]["renterProfile"] == {"persona": "STUDENT"}


@pytest.mark.asyncio
async def test_main_is_chosen_without_boundary_and_explicit_budget_opt_out_stays_respected():
    a = assistant()
    a.planner.plan_result = complete_plan()
    result = await a.try_answer("Tìm phòng trọ ở Gò Vấp, HCM, dưới 3,5 triệu cho 2 người; không thể tăng ngân sách", [], "strict")
    assert result.listing_context["mainRecommendation"]["listingId"] in result.listing_ids
    assert "ngoài ngân sách để cân nhắc" not in result.answer and not a.tools.value_calls
    assert len(a.generative_client.boundary_calls) == 1
    evidence = a.generative_client.boundary_calls[0]
    assert evidence["recommendMain"] is True and evidence["candidates"] == []


@pytest.mark.asyncio
async def test_main_reference_is_separate_from_the_boundary_reference_and_needs_no_new_search():
    a = assistant()
    a.planner.plan_result = complete_plan()
    chat = Conversation(a)
    await chat.ask("Tìm phòng trọ ở Gò Vấp, HCM, dưới 3,5 triệu cho 2 người")
    calls, planner_calls = len(a.tools.payloads), a.planner.calls
    a.tools.read.clear()
    a.tools.details[IDS[0]]["areaM2"] = 28
    result = await chat.ask("Vì sao bạn ưu tiên phòng được chọn?")
    assert result.listing_ids == IDS[:4] and a.tools.read == IDS[:4]
    assert "28 m²" in result.answer and a.generative_client.kwargs is None
    assert "fields" in result.listing_context["mainRecommendation"]
    assert len(a.tools.payloads) == calls and a.planner.calls == planner_calls


@pytest.mark.asyncio
async def test_main_reference_never_reuses_an_older_page_recommendation():
    a = assistant()
    a.planner.plan_result = complete_plan()
    chat = Conversation(a)
    await chat.ask("Tìm phòng trọ ở Gò Vấp, HCM, dưới 3,5 triệu cho 2 người")
    chat.messages.append({"role": "assistant", "content": "Nhóm mới", "status": "ANSWERED",
        "listingContext": a.context(IDS[4:8], chat.state())})
    a.planner.plan_result = ToolPlan("get_listing_details", {"listingIds": IDS[4:8]})
    a.tools.read.clear()
    result = await chat.ask("Vì sao bạn ưu tiên phòng được chọn?")
    assert a.tools.read == IDS[4:8] and IDS[0] not in result.listing_ids


@pytest.mark.asyncio
async def test_main_explanation_uses_its_verified_fields_even_after_a_nearby_comparison():
    a = assistant()
    a.planner.plan_result = complete_plan()
    chat = Conversation(a)
    await chat.ask("Tìm phòng trọ ở Gò Vấp, HCM, dưới 3,5 triệu cho 2 người")
    chat.messages[-1]["listingContext"]["nearby"] = {"radiusMeters": 2000, "groups": ["EDUCATION"]}
    result = await chat.ask("Vì sao bạn chọn phòng đó là phù hợp nhất?")
    assert result.listing_ids == IDS[:4] and not a.tools.nearby_calls
    assert "Mình đã đối chiếu lại" in result.answer and "\n- " in result.answer
    assert a.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_boundary_identifier_cannot_be_selected_as_the_main_recommendation():
    from tests.unit.test_listing_value_advice import rooms, candidate, boundary
    generator = AsyncMock()
    generator.generate_value_advice.return_value = {"selectedId": None,
        "mainRecommendation": {"listingId": IDS[4], "fields": ["areaM2"]}}
    with pytest.raises(ListingToolError, match="Invalid main recommendation"):
        await value.evaluate("Tìm phòng", {}, rooms(), [candidate()], boundary(), generator, recommend_main=True)


def test_profile_is_not_inferred_from_a_search_for_someone_else_or_hypothetical():
    assert guidance.profile_update("Nếu tôi là sinh viên thì nên chọn gì?") == {}
    assert guidance.profile_update("Tìm chỗ ở gần trường đại học") == {}
    assert guidance.profile_update("Mình không phải sinh viên") == {}
    assert guidance.profile_update("Mình tìm giúp bạn là sinh viên") == {}
    assert guidance.profile_update("Mình là kỹ sư", {"persona": "WORKING", "quote": "Mình là kỹ sư"}) == {"persona": "WORKING"}
    assert guidance.profile_update("Tìm phòng", {"persona": "STUDENT", "quote": "Mình là sinh viên"}) == {}
    assert guidance.profile_update("Tôi là người đi làm") == {"persona": "WORKING"}
    assert guidance.profile_update("Tôi tìm nhà cho gia đình") == {"persona": "FAMILY"}
    state = rs.empty_state() | {"renterProfile": {"persona": "FAMILY"}}
    assert not state["context"] and not state["constraints"], "Family never implies children or a budget"


def test_question_metadata_is_public_and_survives_history_with_legacy_default():
    questions = ["Vì sao bạn ưu tiên phòng này?", "Các khoản phí hằng tháng khác nhau thế nào?"]
    result = AskResult(answer="Phòng phù hợp", status="ANSWERED", citations=[], request_id="r", suggested_questions=questions)
    assert public_answer(result).result.suggestedQuestions == questions
    raw = {"id": "m", "role": "assistant", "content": result.answer, "createdAt": "2026-10-30T00:00:00Z"}
    assert ConversationMessage.model_validate(raw | {"suggestedQuestions": questions}).suggestedQuestions == questions
    assert ConversationMessage.model_validate(raw).suggestedQuestions == []


@pytest.mark.asyncio
async def test_persistence_keeps_questions_in_the_same_owned_message():
    repository = ConversationRepository.__new__(ConversationRepository)
    repository.collection = AsyncMock()
    repository.collection.update_one.return_value = SimpleNamespace(matched_count=1)
    assert await repository.append_message("tenant", "conversation", role="assistant", content="Kết quả",
        suggested_questions=["Phòng nào phù hợp hơn?"])
    owned, write = repository.collection.update_one.await_args.args
    assert owned["ownerId"] == "tenant"
    assert write["$push"]["messages"]["suggestedQuestions"] == ["Phòng nào phù hợp hơn?"]
