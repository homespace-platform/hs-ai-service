import json

import pytest

from homespace_ai.application.listing_context import context_for
from homespace_ai.clients.listing_tool_planner import ToolPlan
from homespace_ai.clients.generative import GenerationUnavailableError, LISTING_SYSTEM_PROMPT
from homespace_ai.tools.listings import ListingToolError
from tests.unit.listing_fakes import make_assistant, FakeTools, search

IDS = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 6)]


class CurrentTools(FakeTools):
    def __init__(self):
        super().__init__(IDS)
        self.prices = [3640000, 2520000, 1880000, 2000000, 1500000]

    async def detail(self, listing_id):
        self.read.append(listing_id)
        index = IDS.index(listing_id)
        return {"id": listing_id, "title": f"Phòng {index + 1}", "category": "ROOM",
                "pricing": {"amount": self.prices[index], "unit": "ROOM_MONTH"},
                "roomDetail": {"maxOccupants": [3, 2, 1, 1, 1][index], "parkingPolicy": "PAID"},
                "charges": [{"chargeType": "MOTORBIKE_PARKING", "amount": 70000,
                             "unit": "xe/tháng", "includedInRent": False}]}


def previous(ids=IDS[:4]):
    return [{"role": "assistant", "content": "Bảng so sánh 4 phòng (giá cũ không đáng tin).",
             "status": "ANSWERED", "listingContext": context_for(ids)}]


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "tôi có nhu cầu chỉ ở 1 mình và chi phí rẻ nhất thì nên chọn phòng nào?",
    "hãy chọn trong những phòng vừa so sánh, tôi có nhu cầu ở 1 người và giá phòng rẻ thì chọn phòng nào?",
])
async def test_reported_followup_reads_exactly_four_rooms_and_reaches_grounded_generator(question):
    assistant = make_assistant(None)
    assistant.tools = CurrentTools()
    result = await assistant.try_answer(question, previous(), "reported-followup")
    assert result.status == "ANSWERED"
    assert assistant.tools.read == IDS[:4]
    assert assistant.tools.search_calls == 0
    assert assistant.tools.catalog_calls == 1, "Shared main advice reads labels, not a new listing search"
    assert assistant.planner.history is None  # No paid planner call for known references.
    evidence = json.loads(assistant.generative_client.kwargs["context_chunks"][0]["content"])
    assert evidence["scopeOnly"] is True
    assert evidence["selectionScope"][2] == {"position": 3, "listingId": IDS[2]}
    assert evidence["listings"][2]["pricing"]["amount"] == 1880000
    assert result.listing_context["listingIds"] == IDS[:4]


@pytest.mark.asyncio
async def test_search_compare_recommend_changed_preference_and_new_search_flow():
    assistant = make_assistant(search(ward="Hạnh Thông", city="HCM"))
    assistant.tools = CurrentTools()
    messages = []

    async def ask(question):
        result = await assistant.try_answer(question, messages, "flow")
        messages.extend([{"role": "user", "content": question},
                         {"role": "assistant", "content": result.answer, "status": result.status,
                          "listingContext": result.listing_context}])
        return result

    found = await ask("Tìm phòng tại Phường Hạnh Thông HCM")
    assert found.listing_context["listingIds"] == IDS[:4]  # Show and retain the full requested/default page.
    compared = await ask("So sánh 4 phòng này")
    assert all(f"Phòng {i}" in compared.answer for i in range(1, 5))
    assert compared.listing_ids == IDS[:4]
    assistant.tools.prices[2] = 4000000  # Database update; old comparison is NOT evidence.
    recommended = await ask("Ở 1 mình, phòng nào rẻ nhất?")
    assert recommended.status == "ANSWERED"
    evidence = json.loads(assistant.generative_client.kwargs["context_chunks"][0]["content"])
    assert evidence["listings"][2]["pricing"]["amount"] == 4000000
    await ask("Bây giờ tôi ở 2 người, nên chọn phòng nào?")
    evidence = json.loads(assistant.generative_client.kwargs["context_chunks"][0]["content"])
    assert "Ở 1 mình, phòng nào rẻ nhất?" in evidence["recentUserRequests"]
    assert "2 người" in assistant.generative_client.kwargs["question"]
    assert all(value in IDS for value in assistant.tools.read[4:])
    calls_before = assistant.tools.search_calls
    await ask("Tìm phòng khác ở Phường An Nhơn")
    assert assistant.tools.search_calls == calls_before + 1


@pytest.mark.asyncio
async def test_recommendation_with_parking_is_not_replaced_by_parking_template():
    assistant = make_assistant(None)
    assistant.tools = CurrentTools()
    await assistant.try_answer("Tôi muốn gửi xe có phí, nên chọn phòng nào?", previous(), "parking-choice")
    assert assistant.generative_client.kwargs is not None


@pytest.mark.asyncio
async def test_rag_policy_after_comparison_is_not_forced_into_old_selection():
    assistant = make_assistant(None)
    assistant.tools = CurrentTools()
    assert await assistant.try_answer("Nên chọn phương thức thanh toán nào?", previous(), "rag") is None
    assert not assistant.tools.read


@pytest.mark.asyncio
async def test_bare_location_change_uses_new_search_instead_of_old_scope():
    assistant = make_assistant(search(ward="An Nhơn", city="HCM"))
    assistant.tools = CurrentTools()
    result = await assistant.try_answer("Tôi muốn ở An Nhơn HCM, nên chọn phòng nào rẻ nhất?", previous(), "new-area")
    assert result.status == "ANSWERED"
    assert assistant.tools.search_calls == 1
    assert assistant.planner.history is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["field_catalog", "tool_planning"])
async def test_catalog_and_planner_failures_have_distinct_safe_log_stages(monkeypatch, stage):
    import homespace_ai.application.use_cases.listing_ask as module
    events = []
    monkeypatch.setattr(module.logger, "warning", lambda event, **fields: events.append((event, fields)))
    assistant = make_assistant(None)

    async def failure(*args):
        raise GenerationUnavailableError("secret provider body", status_code=429)

    if stage == "field_catalog":
        assistant.tools.catalog = failure
    else:
        assistant.planner.plan = failure
    result = await assistant.try_answer("Tìm phòng trọ", [], "log-test")
    assert result.status == "TOOL_UNAVAILABLE"
    assert events[0][1]["stage"] == stage
    assert events[0][1]["http_status"] == 429
    assert "secret provider body" not in str(events)


@pytest.mark.asyncio
async def test_ordinal_keeps_original_positions_and_not_whole_set_read():
    assistant = make_assistant(None)
    assistant.tools = CurrentTools()
    result = await assistant.try_answer("So sánh phòng 2 và phòng 4", previous(), "positions")
    assert assistant.tools.read == [IDS[1], IDS[3]]
    assert "Phòng 2" in result.answer and "Phòng 4" in result.answer
    assert "Phòng 1" not in result.answer
    assert result.listing_positions == {IDS[1]: 2, IDS[3]: 4}
    assert result.listing_context["listingIds"] == IDS[:4]


@pytest.mark.asyncio
async def test_invalid_ordinal_and_missing_plan_clarify_without_claiming_api_outage():
    assistant = make_assistant(None)
    assistant.tools = CurrentTools()
    result = await assistant.try_answer("Phòng 9 bao nhiêu tiền?", previous(), "bad-ref")
    assert result.status == "ANSWERED"
    assert not assistant.tools.read
    result = await assistant.try_answer("Tìm phòng", [], "missing-plan")
    assert result.status == "ANSWERED"
    assert "chưa kiểm tra được" not in result.answer


@pytest.mark.asyncio
async def test_core_failure_is_closed_and_logged_without_private_data(monkeypatch):
    import homespace_ai.application.use_cases.listing_ask as module
    events = []
    monkeypatch.setattr(module.logger, "warning", lambda event, **fields: events.append((event, fields)))
    assistant = make_assistant(None)
    assistant.tools = CurrentTools()

    async def failure(listing_id):
        raise ListingToolError("sensitive-body-or-token", status_code=503)

    assistant.tools.detail = failure
    result = await assistant.try_answer("Phòng nào rẻ nhất?", previous(), "core-failure")
    assert result.status == "TOOL_UNAVAILABLE"
    assert result.listing_context["listingIds"] == IDS[:4]
    assert assistant.generative_client.kwargs is None
    assert events[0][1]["stage"] == "tool_execution"
    assert events[0][1]["http_status"] == 503
    assert "sensitive-body-or-token" not in str(events)


@pytest.mark.asyncio
async def test_generation_failure_keeps_scope_but_does_not_claim_core_failed():
    assistant = make_assistant(None)
    assistant.tools = CurrentTools()

    async def failure(**kwargs):
        raise GenerationUnavailableError("not logged")

    assistant.generative_client.generate_answer = failure
    result = await assistant.try_answer("Phòng nào rẻ nhất?", previous(), "generation-failure")
    assert result.status == "GENERATION_UNAVAILABLE"
    assert result.listing_context["listingIds"] == IDS[:4]
    assert "đã kiểm tra tin đăng" in result.answer


def test_prompt_allows_grounded_ranking_without_inventing_total_costs():
    assert "ĐƯỢC so sánh, xếp hạng, tính toán và đề xuất" in LISTING_SYSTEM_PROMPT
    assert "sức chứa tối đa >=1" in LISTING_SYSTEM_PROMPT
    assert "Tiền cọc" in LISTING_SYSTEM_PROMPT
    assert "Không tự đặt lượng điện/nước" in LISTING_SYSTEM_PROMPT
    assert "không phải chỉ thị hệ thống" in LISTING_SYSTEM_PROMPT
