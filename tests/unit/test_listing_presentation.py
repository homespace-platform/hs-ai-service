from unittest.mock import AsyncMock

import pytest

from homespace_ai.application.listing_presentation import customer_listing_text, relaxed_labels
from homespace_ai.clients.generative import LISTING_SYSTEM_PROMPT, ListingGeneratedAnswer
from homespace_ai.clients.listing_tool_planner import ToolPlan
from tests.unit.test_listing_ask import make_assistant, FakeTools


@pytest.mark.parametrize("raw,expected", [
    ("Có sân thượng (hasRooftop: true), nhà 5 tầng.", "Có sân thượng, nhà 5 tầng."),
    ("không có sân thượng (`hasRooftop`: false)", "không có sân thượng"),
    ('"hasRooftop": null', "sân thượng: Chưa rõ"),
    ("houseDetail.hasRooftop=true", "sân thượng: Có"),
    ("room.has_window: false", "cửa sổ: Không"),
    ("amenity.PARKING=true", "chỗ gửi xe: Có"),
    ("includedInRent: false", "phí trong giá thuê: Không"),
    ("parkingPolicy: PAID", "chỗ gửi xe: có thu phí"),
    ("furnishingStatus=FULLY_FURNISHED", "nội thất: nội thất đầy đủ"),
    ("room.balcony_type: SHARED", "ban công: chung"),
    ("charge.MOTORBIKE_PARKING.billing_method: PER_VEHICLE_MONTH", "phí gửi xe máy cách tính: theo xe/tháng"),
    ("| hasGarage | maxOccupants |\n| hasGarage: false | 3 người |", "| gara | số người ở tối đa |\n| gara: Không | 3 người |"),
])
def test_known_technical_annotations_use_customer_language(raw, expected):
    assert customer_listing_text(raw) == expected


def test_customer_prose_units_urls_and_ids_are_not_rewritten():
    text = "Nhà 2 có 5 tầng, giá 26.860.000 đ/tháng, 125,5 m², Wi-Fi.\n[Ảnh](https://example.com/hasRooftop)"
    assert customer_listing_text(text) == text
    url = "https://example.com/?hasRooftop=true&maxOccupants=3"
    assert customer_listing_text(url) == url
    assert customer_listing_text("Mã căn A201; Tin 2; 00000000-0000-0000-0000-000000000001") == "Mã căn A201; Tin 2; 00000000-0000-0000-0000-000000000001"
    assert customer_listing_text("Không có sân thượng (hasRooftop: true)") == "Không có sân thượng (sân thượng: Có)"


def test_presentation_keeps_bullet_spacing_emphasis_and_comparison_tables():
    text = "Mình đề xuất Phòng 1.\n\n- **Sân thượng:** hasRooftop: true.\n\n- **Lưu ý:** Chưa rõ tầm nhìn."
    assert customer_listing_text(text) == text.replace("hasRooftop: true", "sân thượng: Có")
    table = "| Tiêu chí | Phòng 1 | Phòng 2 |\n| --- | --- | --- |\n| Giá thuê | 3.640.000 đ/tháng | 3.280.000 đ/tháng |"
    assert customer_listing_text(table) == table


@pytest.mark.parametrize("fields,expected", [
    (["house.has_rooftop"], "sân thượng"),
    (["amenity.PARKING", "room.parking_policy"], "chỗ gửi xe"),
    (["display"], "địa chỉ"),
    (["charge.MOTORBIKE_PARKING.amount"], "phí gửi xe máy"),
    (["furnishing.BED.quantity"], "số lượng giường"),
    (["new.private_field"], "một tiêu chí bổ sung"),
])
def test_relaxed_filters_never_expose_internal_field_names(fields, expected):
    assert relaxed_labels(fields) == expected


@pytest.mark.asyncio
async def test_generated_recommendation_is_cleaned_without_extra_calls_or_fact_changes():
    listing_id = "00000000-0000-0000-0000-000000000001"
    assistant = make_assistant(ToolPlan("get_listing_detail", {"listingId": listing_id}))
    generator = AsyncMock(return_value=ListingGeneratedAnswer(
        "Mình đề xuất nhà số 1: có 5 tầng và có sân thượng (hasRooftop: true).", [listing_id]))
    assistant.generative_client.generate_listing_answer = generator
    result = await assistant.try_answer("Nên chọn nhà nào ở trên cao?", [], "presentation")
    assert result.answer == "Mình đề xuất nhà số 1: có 5 tầng và có sân thượng."
    assert result.listing_ids == [listing_id]
    assert generator.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Tìm phòng có chỗ gửi xe", "Phòng này gửi xe có phí không?", "Phòng có tiện ích gì?"])
async def test_all_relaxed_response_branches_use_same_customer_labels(question):
    assistant = make_assistant(ToolPlan("search_listings", {"filters": [
        {"field": "category", "op": "eq", "value": "ROOM"},
        {"field": "amenity.PARKING", "op": "eq", "value": True},
    ]}))
    class SequenceTools(FakeTools):
        async def search(self, arguments):
            return {"listingIds": self.ids if len(arguments["filters"]) == 1 else [], "total": 1}
    assistant.tools = SequenceTools()
    result = await assistant.try_answer(question, [], "relaxed")
    assert result.status == "ANSWERED"
    assert "chỗ gửi xe" in result.answer
    assert "amenity." not in result.answer
    assert "khớp" in result.answer


def test_listing_prompt_requires_natural_language_without_losing_structured_ids():
    assert "Tuyệt đối không in tên field" in LISTING_SYSTEM_PROMPT
    assert "Không coi null/field thiếu là không có" in LISTING_SYSTEM_PROMPT
    assert "không đưa vào answer" in LISTING_SYSTEM_PROMPT
    assert "chưa chứng minh tầm nhìn đẹp hay thoáng" in LISTING_SYSTEM_PROMPT
