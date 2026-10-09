from types import SimpleNamespace

import pytest

from homespace_ai.application.use_cases.listing_ask import ListingAsk
from homespace_ai.clients.generative import GenerationUnavailableError
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
    assert "chưa thấy tin khớp đủ mọi tiêu chí" in result.answer
    assert "nới điều kiện ban công" in result.answer


@pytest.mark.asyncio
async def test_browse_answer_only_links_matching_category_and_skips_unasked_fields():
    room_id = "00000000-0000-0000-0000-000000000001"
    house_id = "00000000-0000-0000-0000-000000000002"
    assistant = make_assistant(ToolPlan("search_listings", {"filters": [
        {"field": "category", "op": "eq", "value": "ROOM"},
        {"field": "ward_name", "op": "contains", "value": "Phú Lợi"},
    ]}), ids=[room_id, house_id])
    class MixedTools(FakeTools):
        async def detail(self, listing_id):
            return {"id": listing_id, "title": "Phòng Phú Lợi" if listing_id == room_id else "Nhà Phú Lợi",
                    "category": "ROOM" if listing_id == room_id else "HOUSE", "areaM2": 23,
                    "pricing": {"amount": 3360000, "unit": "ROOM_MONTH", "depositMonths": 3},
                    "address": {"wardName": "Phường Phú Lợi"},
                    "roomDetail": {"balconyType": "PRIVATE"}}
    assistant.tools = MixedTools([room_id, house_id])
    result = await assistant.try_answer("Tìm phòng khu vực Phú Lợi", [], "req-browse")
    assert result.status == "ANSWERED"
    assert "/rent/" + room_id in result.answer
    assert "/rent/" + house_id not in result.answer
    assert "3.360.000 đ/phòng/tháng" in result.answer
    assert "23 m²" in result.answer
    assert "đặt cọc" not in result.answer.lower()
    assert "Tiện ích:" not in result.answer
    assert assistant.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_general_room_comparison_is_complete_compact_markdown_table():
    ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 5)]
    assistant = make_assistant(ToolPlan("get_listing_details", {"listingIds": ids}), ids=ids)

    class ComparisonTools(FakeTools):
        async def detail(self, listing_id):
            index = ids.index(listing_id)
            return {"id": listing_id, "title": f"Phòng thử nghiệm {index + 1}",
                    "category": "ROOM", "areaM2": 20 + index,
                    "pricing": {"amount": 2000000 + index * 100000,
                                "unit": "ROOM_MONTH"},
                    "roomDetail": {"maxOccupants": index + 1, "restroomType": "PRIVATE",
                                   "kitchenType": "SHARED", "hasWindow": True,
                                   "balconyType": "NONE", "hasMezzanine": False,
                                   "furnishingStatus": "BASIC", "maxVehicles": 1,
                                   "parkingPolicy": "FREE" if index == 0 else "PAID"},
                    "charges": [] if index == 0 else [{"chargeType": "MOTORBIKE_PARKING",
                                                         "amount": 90000, "unit": "xe/tháng",
                                                         "includedInRent": False}]}

    assistant.tools = ComparisonTools(ids)
    result = await assistant.try_answer("Tôi muốn so sánh 4 phòng trên", [], "req-compare")

    assert result.status == "ANSWERED"
    assert "| Tiêu chí |" in result.answer
    assert "| Giá thuê |" in result.answer
    assert "| Ban công |" in result.answer
    assert "không thu phí riêng" in result.answer
    assert "90.000 đ/xe/tháng" in result.answer
    assert all(f"/rent/{listing_id}" in result.answer for listing_id in ids)
    assert "ID:" not in result.answer
    assert assistant.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_specific_room_comparison_still_uses_model_for_requested_field():
    ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 3)]
    assistant = make_assistant(ToolPlan("get_listing_details", {"listingIds": ids}), ids=ids)
    result = await assistant.try_answer("So sánh phí điện của 2 phòng trên", [], "req-compare-fees")
    assert result.status == "ANSWERED"
    assert assistant.generative_client.kwargs is not None


@pytest.mark.asyncio
async def test_generation_failure_after_successful_tool_read_does_not_claim_tool_failed():
    assistant = make_assistant(ToolPlan("get_listing_detail", {
        "listingId": "00000000-0000-0000-0000-000000000001"}))

    class FailedGenerator:
        async def generate_answer(self, **kwargs):
            raise GenerationUnavailableError("answer was cut off")

    assistant.generative_client = FailedGenerator()
    result = await assistant.try_answer("Phòng này có tiện ích gì?", [], "req-truncated")
    assert result.status == "GENERATION_UNAVAILABLE"
    assert "đã kiểm tra tin đăng" in result.answer
    assert "chưa kiểm tra được" not in result.answer


@pytest.mark.asyncio
async def test_general_question_stays_in_rag():
    assistant = make_assistant(None)
    assert await assistant.try_answer("HomeSpace là gì?", [], "req-3") is None
    assert await assistant.try_answer("Cách đặt lịch xem phòng trên HomeSpace?", [], "req-3b") is None
