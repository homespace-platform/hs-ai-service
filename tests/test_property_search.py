import os

import pytest

from homespace_ai.property_search.intent import fallback_intent
from homespace_ai.property_search.mcp_client import call_listing_tool
from homespace_ai.property_search.response import (
    compose_search_reply,
    previous_result_ids,
    references_previous_results,
)
from homespace_ai.clients.generative import DisabledGenerativeClient
from homespace_ai.clients.generative import BaseGenerativeClient


def test_vietnamese_fallback_extracts_room_features_and_budget():
    intent = fallback_intent("Tôi là sinh viên tìm phòng trọ có gác, ban công giá dưới 3tr")
    assert intent.category == "ROOM"
    assert intent.price_max == 3_000_000
    assert intent.has_mezzanine is True
    assert intent.has_balcony is True


def test_fallback_keeps_go_vap_without_inventing_distance():
    intent = fallback_intent("trọ gần đại học công nghiệp gò vấp, có gác dưới 3tr")
    assert intent.location == "Gò Vấp"
    assert intent.landmark is not None


def test_follow_up_uses_only_previous_search_results():
    history = [{
        "role": "assistant", "status": "PROPERTY_SEARCH",
        "content": "- [Tin A](/rent/c3822603-ae07-e05c-a8f3-ec2da14e0352)",
    }]
    ids = previous_result_ids(None, history)
    assert ids == ["c3822603-ae07-e05c-a8f3-ec2da14e0352"]
    assert references_previous_results(
        "Trong khu vực đó còn phòng nào có chỗ gửi xe không?", bool(ids), False
    )
    assert not references_previous_results("Tìm phòng ở Gò Vấp", bool(ids), True)


@pytest.mark.asyncio
async def test_parking_follow_up_answers_about_shown_listings_without_generic_template():
    result = {"total": 2, "matches": [
        {"id": "c3822603-ae07-e05c-a8f3-ec2da14e0352", "title": "Phòng A",
         "category": "ROOM", "price": 3_200_000, "parkingPolicy": "FREE", "maxVehicles": 9},
        {"id": "f199ecd2-5191-4d34-91c8-81a1f5a7e343", "title": "Phòng B",
         "category": "ROOM", "price": 3_200_000, "parkingPolicy": "PAID", "maxVehicles": 0},
    ]}
    reply = await compose_search_reply(
        question="Trong khu vực đó còn phòng nào có chỗ gửi xe không?",
        result_data=result,
        previous_ids=[item["id"] for item in result["matches"]],
        scoped=True,
        district="Phường Bình Thạnh",
        generative_client=DisabledGenerativeClient(),
    )
    assert "hai tin mình vừa gửi, có một tin ghi có chỗ gửi xe" in reply
    assert "Riêng tin “Phòng B”" in reply
    assert "tối đa 9 xe" in reply
    assert "chưa xác nhận chỗ gửi xe (chính sách thu phí nhưng tối đa 0 xe)" in reply
    assert "gửi xe miễn phí" in reply
    assert "gửi xe có thu phí" not in reply
    assert "Bạn muốn mình lọc tiếp" not in reply


class _HallucinatingClient(BaseGenerativeClient):
    async def generate_answer(self, question, context_chunks, *, audience="USER", mode="homespace"):
        return "Cả hai đều có chỗ gửi xe miễn phí và trả phí."


@pytest.mark.asyncio
async def test_initial_search_rejects_unasked_parking_claim():
    reply = await compose_search_reply(
        question="Tìm phòng trọ có gác và ban công dưới 3,5 triệu ở Bình Thạnh",
        result_data={"total": 1, "matches": [{
            "id": "c3822603-ae07-e05c-a8f3-ec2da14e0352", "title": "Phòng A",
            "category": "ROOM", "price": 3_200_000, "parkingPolicy": "FREE", "maxVehicles": 9,
        }]},
        previous_ids=[], scoped=False, district="Phường Bình Thạnh",
        generative_client=_HallucinatingClient(),
    )
    assert "gửi xe" not in reply
    assert "Phòng A" in reply


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_LIVE_LISTING_TESTS") != "1", reason="requires seeded homespace_core")
async def test_mcp_queries_live_published_listings():
    result = await call_listing_tool("search_listings", {
        "province_code": "79",
        "category": "ROOM",
        "price_max": 3_000_000,
        "has_mezzanine": True,
        "has_balcony": True,
    })
    assert result["total"] >= 0
    assert isinstance(result["listing_ids"], list)
    suggestions = await call_listing_tool("suggest_listing_places", {"province_code": "79"})
    assert isinstance(suggestions["suggestions"], list)
    fallback_suggestions = await call_listing_tool("suggest_listing_places", {
        "province_code": "01",
        "district": "Ba Đình",
    })
    assert fallback_suggestions["fallbackToProvince"] is True
    assert fallback_suggestions["suggestions"]
    ward = await call_listing_tool("resolve_listing_ward", {
        "province_code": "79",
        "query": "trọ gần đại học công nghiệp Gò Vấp",
    })
    assert ward["ward"] == "Phường Gò Vấp"
