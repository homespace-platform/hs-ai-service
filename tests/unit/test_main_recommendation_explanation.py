"""Fresh whole-shortlist explanations: model selects fields, application verifies comparisons."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from homespace_ai.application.listing_recommendation_explanation import explain
from homespace_ai.application.listing_catalog import SearchCatalog
from homespace_ai.application import listing_value_advice as value
from homespace_ai.clients.generative import GenerationUnavailableError, OpenAIGenerativeClient
from homespace_ai.tools.listings import ListingToolError
from tests.unit.listing_fakes import CATALOG, IDS, room
from tests.unit.test_openai_generation import completed, mock_http, settings

CAT = SearchCatalog(CATALOG)


def shortlist():
    return [room(IDS[i], areaM2=area, pricing={"amount": price, "currency": "VND", "unit": "ROOM_MONTH", "managementFeeIncluded": True},
                 roomDetail={"restroomType": wc, "maxOccupants": 3}, amenities=[{"code": "WIFI", "name": "WiFi"}])
            for i, (area, price, wc) in enumerate([(37, 4300000, "PRIVATE"), (33, 4000000, "PRIVATE"),
                                                  (30, 3300000, "SHARED"), (35, 4400000, "PRIVATE")])]


def generator(reasons=None, tradeoffs=None):
    client = AsyncMock()
    client.generate_main_explanation.return_value = {"reasons": reasons or ["areaM2", "roomDetail.restroomType"],
                                                     "tradeoffs": tradeoffs or ["pricing.amount"]}
    return client


@pytest.mark.asyncio
async def test_why_rereads_all_four_and_explains_real_benefits_and_tradeoffs_in_bullets():
    client = generator()
    result = await explain("Vì sao bạn ưu tiên phòng đó?", {"listingId": IDS[0], "fields": ["pricing.managementFeeIncluded"]},
        shortlist(), {"context": {"occupants": 3}}, IDS[:4], CAT, client)
    evidence = client.generate_main_explanation.await_args.args[1]
    assert [row["id"] for row in evidence["listings"]] == IDS[:4]
    assert evidence["factMatrix"]["areaM2"]["values"] == ["37 m²", "33 m²", "30 m²", "35 m²"]
    assert result.count("\n- ") == 3
    for number in (37, 33, 30, 35):
        assert f"{number} m²" in result
    for identifier in IDS[:4]:
        assert f"/rent/{identifier}" in result
    assert "Không gian rộng hơn các phòng còn lại" in result
    assert "Điểm cần cân nhắc" in result and "4.300.000 đ/tháng" in result and "3.300.000 đ/tháng" in result
    assert "managementFeeIncluded" not in result and "theo thông tin hiện công bố" not in result


@pytest.mark.asyncio
async def test_a_feature_shared_by_all_is_not_claimed_as_a_unique_advantage():
    items = shortlist()
    client = generator(["amenities.WIFI"], [])
    result = await explain("Tại sao phòng này?", {"listingId": IDS[0]}, items, {}, IDS[:4], CAT, client)
    assert "WiFi" not in result
    assert "thuận lợi hơn so với các phòng còn lại" not in result


@pytest.mark.asyncio
async def test_mixed_size_comparison_never_hides_a_better_room():
    items = shortlist()
    items[1]["areaM2"] = 45
    result = await explain("Tại sao phòng này?", {"listingId": IDS[0]}, items, {}, IDS[:4], CAT, generator(["areaM2"]))
    assert "45 m²" in result and "không vượt trội cả nhóm" in result
    assert "Không gian rộng hơn các phòng còn lại" not in result


@pytest.mark.asyncio
async def test_missing_facts_are_not_absent_features_and_old_facts_are_not_replayed():
    items = shortlist()
    for item in items[1:]:
        item.pop("areaM2")
    items[0]["areaM2"] = 39
    client = generator(["areaM2"])
    result = await explain("Vì sao?", {"listingId": IDS[0], "fields": ["areaM2"]}, items, {}, IDS[:4], CAT, client)
    assert "39 m²" in result and "chưa có đủ thông tin để đối chiếu" in result
    assert "Không gian rộng hơn" not in result


@pytest.mark.asyncio
async def test_contextual_differences_are_compared_without_calling_them_missing_or_better():
    items = shortlist()
    for index, item in enumerate(items):
        item["roomDetail"]["accessHoursType"] = "FLEXIBLE" if index==0 else "CURFEW"
    result = await explain("Vì sao?", {"listingId": IDS[0]}, items, {}, IDS[:4], CAT,
                           generator(["roomDetail.accessHoursType"]))
    assert "Giờ giấc tự do" in result and "Có giờ đóng cửa" in result
    assert "thói quen sinh hoạt" in result and "chưa có đủ thông tin để đối chiếu" not in result


@pytest.mark.asyncio
async def test_optional_model_outage_keeps_verified_whole_group_fallback():
    client = generator()
    client.generate_main_explanation.side_effect = GenerationUnavailableError("offline", retryable=False)
    result = await explain("Vì sao?", {"listingId": IDS[0], "fields": ["areaM2"]}, shortlist(), {}, IDS[:4], CAT, client)
    assert "37 m²" in result and "35 m²" in result and "\n- " in result


@pytest.mark.asyncio
async def test_missing_selected_listing_never_switches_to_another_room():
    client = generator()
    assert await explain("Vì sao?", {"listingId": IDS[4]}, shortlist(), {}, IDS[:4], CAT, client) is None
    client.generate_main_explanation.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_model_fields_are_not_rendered():
    with pytest.raises(ListingToolError):
        await explain("Vì sao?", {"listingId": IDS[0]}, shortlist(), {}, IDS[:4], CAT, generator(["unknown.invented"]))


def test_short_recommendation_uses_customer_labels_instead_of_raw_keys_and_boolean_dump():
    decision = {"listingId": IDS[0], "reasons": [
        {"field": "amenities.WIFI", "label": "WiFi", "display": "Có", "value": True},
        {"field": "pricing.managementFeeIncluded", "label": "Phí quản lý", "display": "Có", "value": True}]}
    text = value.render_main(decision, shortlist())
    assert "có **WiFi**" in text and "giá đã gồm **phí quản lý**" in text
    assert "managementFeeIncluded" not in text and ": **Có**" not in text


@pytest.mark.asyncio
async def test_explanation_model_uses_bounded_strict_schema_and_the_listing_model(monkeypatch):
    calls = mock_http(monkeypatch, [httpx.Response(200, json=completed(json.dumps({"reasons": ["areaM2"], "tradeoffs": []})))])
    client = OpenAIGenerativeClient(settings())
    await explain("Vì sao?", {"listingId": IDS[0]}, shortlist(), {}, IDS[:4], CAT, client)
    payload = json.loads(calls[0].content)
    assert payload["model"] == "gpt-4.1-mini" and payload["max_output_tokens"] <= 1600
    schema = payload["text"]["format"]
    assert schema["strict"] and schema["schema"]["properties"]["reasons"]["maxItems"] == 3
    assert "areaM2" in schema["schema"]["properties"]["reasons"]["items"]["enum"]


@pytest.mark.asyncio
async def test_main_explanation_truncation_does_not_retry_and_falls_back_to_fresh_data(monkeypatch):
    calls = mock_http(monkeypatch, [httpx.Response(200, json={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}})])
    result = await explain("Vì sao?", {"listingId": IDS[0], "fields": ["areaM2"]}, shortlist(), {}, IDS[:4], CAT, OpenAIGenerativeClient(settings()))
    assert len(calls) == 1 and "37 m²" in result and "33 m²" in result
