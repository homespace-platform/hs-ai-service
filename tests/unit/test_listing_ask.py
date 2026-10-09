from types import SimpleNamespace

import pytest

from homespace_ai.application.use_cases.listing_ask import ListingAsk
from homespace_ai.clients.listing_tool_planner import ToolPlan


class FakeTools:
    def __init__(self, ids=None):
        self.ids = ids if ids is not None else ["00000000-0000-0000-0000-000000000001"]

    async def fields(self):
        return {"room.parking_policy": "TEXT", "charge.MOTORBIKE_PARKING.amount": "NUMBER"}

    async def search(self, arguments):
        return {"listingIds": self.ids, "total": len(self.ids), "readAt": "2026-10-09T00:00:00Z"}

    async def detail(self, listing_id):
        return {"id": listing_id, "title": "Phòng thử nghiệm", "category": "ROOM",
                "roomDetail": {"parkingPolicy": "PAID"},
                "charges": [{"chargeType": "MOTORBIKE_PARKING", "amount": 80000,
                             "includedInRent": False}]}


class FakePlanner:
    def __init__(self, plan):
        self.plan_result = plan
        self.history = None

    async def plan(self, question, history, fields):
        self.history = history
        return self.plan_result


class FakeGenerator:
    def __init__(self):
        self.kwargs = None

    async def generate_answer(self, **kwargs):
        self.kwargs = kwargs
        return "Phòng này có chỗ gửi xe có phí 80.000 đ/tháng."


def make_assistant(plan, ids=None):
    assistant = ListingAsk.__new__(ListingAsk)
    assistant.settings = SimpleNamespace()
    assistant.tools = FakeTools(ids)
    assistant.planner = FakePlanner(plan)
    assistant.generative_client = FakeGenerator()
    return assistant


@pytest.mark.asyncio
async def test_listing_question_uses_core_data_and_preserves_history():
    plan = ToolPlan("search_listings", {"filters": [{"field": "room.parking_policy", "op": "eq", "value": "PAID"}]})
    assistant = make_assistant(plan)
    history = [{"role": "assistant", "content": "Mình vừa tìm được một phòng."}]
    result = await assistant.try_answer("Phòng đó gửi xe có phí không?", history, "req-1")
    assert result.status == "ANSWERED"
    assert "/rent/00000000-0000-0000-0000-000000000001" in result.answer
    assert "có thu phí" in result.answer
    assert "80.000" in result.answer
    assert "miễn phí" not in result.answer
    assert assistant.planner.history == history
    assert assistant.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_no_exact_match_is_not_relabelled_as_rag_answer():
    assistant = make_assistant(ToolPlan("search_listings", {"filters": []}), ids=[])
    result = await assistant.try_answer("Tìm phòng", [], "req-2")
    assert result.status == "NO_RESULTS"
    assert assistant.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_non_parking_fields_use_grounded_core_evidence():
    assistant = make_assistant(ToolPlan("get_listing_detail", {"listingId": "00000000-0000-0000-0000-000000000001"}))
    result = await assistant.try_answer("Phòng này có những tiện ích gì?", [], "req-4")
    assert result.status == "ANSWERED"
    assert '"parkingPolicy": "PAID"' in assistant.generative_client.kwargs["context_chunks"][0]["content"]
    assert assistant.generative_client.kwargs["mode"] == "listing"


@pytest.mark.asyncio
async def test_related_suggestion_is_explicitly_not_exact_match():
    assistant = make_assistant(ToolPlan("search_listings", {"filters": [
        {"field": "category", "op": "eq", "value": "ROOM"},
        {"field": "room.has_balcony", "op": "eq", "value": "true"},
    ]}))
    class SequenceTools(FakeTools):
        def __init__(self):
            super().__init__()
            self.calls = 0
        async def search(self, arguments):
            self.calls += 1
            return {"listingIds": [] if self.calls == 1 else self.ids,
                    "total": 0 if self.calls == 1 else 1, "readAt": "now"}
    assistant.tools = SequenceTools()
    result = await assistant.try_answer("Tìm phòng có ban công", [], "req-5")
    assert result.status == "ANSWERED"
    assert "Chưa có tin khớp toàn bộ điều kiện" in result.answer
    assert "room.has_balcony" in result.answer


@pytest.mark.asyncio
async def test_general_question_stays_in_rag():
    assistant = make_assistant(None)
    assert await assistant.try_answer("HomeSpace là gì?", [], "req-3") is None
    assert await assistant.try_answer("Cách đặt lịch xem phòng trên HomeSpace?", [], "req-3b") is None
