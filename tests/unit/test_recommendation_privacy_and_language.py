"""Customer-facing advice: privacy, real distinctions and natural verified sentences."""
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from homespace_ai.application import listing_value_advice as advice
from homespace_ai.application.listing_catalog import SearchCatalog
from homespace_ai.application.listing_recommendation_explanation import explain
from homespace_ai.application.listing_presentation import without_managed_choice
from homespace_ai.clients.generative import OpenAIGenerativeClient, GenerationUnavailableError
from homespace_ai.tools.listings import ListingToolError
from tests.unit.listing_fakes import CATALOG, IDS, room
from tests.unit.test_openai_generation import completed, mock_http, settings

CAT = SearchCatalog(CATALOG)


def displayed_rooms():
    return [room(IDS[i], areaM2=area, pricing={"amount": rent, "currency": "VND", "unit": "ROOM_MONTH", "managementFeeIncluded": True},
        roomDetail={"restroomType": wc, "kitchenType": kitchen, "maxOccupants": 3, "hasWindow": True},
        amenities=[{"code": "WIFI", "name": "WiFi"}],
        charges=[{"chargeType": "SERVICE_OR_GARBAGE", "includedInRent": False, "amount": 30000, "billingMethod": "PER_PERSON_MONTH"}])
        for i,(area,rent,wc,kitchen) in enumerate([(30,3800000,"SHARED","PRIVATE"),(32,3900000,"SHARED","SHARED"),
                                                   (30,3300000,"PRIVATE","NONE"),(37,3800000,"PRIVATE","NONE")])]


async def evaluate(main, criteria=None, listings=None):
    client = AsyncMock()
    client.generate_value_advice.return_value = {"selectedId": None, "advantages": [], "tradeoffs": [],
        "mainRecommendation": main}
    result = await advice.evaluate("Tôi là sinh viên cần chỗ cho 3 người dưới 4 triệu", criteria or {}, listings or displayed_rooms(),
        [], {"candidateIds": []}, client, CAT, recommend_main=True)
    return result,client.generate_value_advice.await_args.kwargs["evidence"]


@pytest.mark.asyncio
async def test_student_advice_defaults_to_privacy_and_excludes_shared_facts_from_positive_reasons():
    result,evidence = await evaluate({"listingId": IDS[2], "fields": ["roomDetail.restroomType", "pricing.managementFeeIncluded"]},
                                     {"renterProfile": {"persona": "STUDENT"}})
    assert evidence["mainRecommendationAllowedIds"] == [IDS[0], IDS[2], IDS[3]]
    for item in evidence["mainListings"]:
        assert not {"amenities.WIFI", "pricing.managementFeeIncluded", "roomDetail.maxOccupants", "roomDetail.hasWindow"} & set(item["usefulReasonFields"])
    assert "roomDetail.restroomType" not in evidence["mainListings"][1]["usefulReasonFields"]
    assert [r["field"] for r in result["main"]["reasons"]] == ["roomDetail.restroomType"]
    text = advice.render_main(result["main"], displayed_rooms())
    assert "có **nhà vệ sinh riêng**" in text and "phí quản lý" not in text and "chung" not in text


@pytest.mark.asyncio
async def test_invalid_shared_wc_selection_is_not_allowed_to_slip_through_a_model_response():
    with pytest.raises(ListingToolError,match="privacy"):
        await evaluate({"listingId": IDS[1], "fields": ["roomDetail.restroomType", "pricing.managementFeeIncluded"]})


@pytest.mark.parametrize("criteria", [
    {"valuePriorities": [{"field": "area_m2", "direction": "HIGHER", "quote": "ưu tiên rộng"}]},
    {"valuePriorities": [{"field": "room.restroom_type", "direction": "IGNORE", "quote": "WC chung cũng được"}]},
    {"constraints": [{"field": "room.kitchen_type", "strength": "preference", "op": "eq", "value": "PRIVATE"}]},
    {"sort": "price_asc"},
])
def test_explicit_priorities_remain_the_customers_choice(criteria):
    assert advice.main_selection_ids(displayed_rooms(),criteria) == IDS[:4]


def test_ignoring_furniture_does_not_remove_the_general_privacy_default():
    assert advice.main_selection_ids(displayed_rooms(),{"valuePriorities":[{"field":"room.furnishing_status","direction":"IGNORE"}]}) == [IDS[0], IDS[2], IDS[3]]


@pytest.mark.asyncio
async def test_shared_wc_can_meet_budget_but_is_never_a_positive_privacy_reason():
    items=displayed_rooms()
    for item in items:
        item["roomDetail"]["restroomType"]="SHARED"
    result,_=await evaluate({"listingId": IDS[2], "fields": ["roomDetail.restroomType", "pricing.amount"]},listings=items)
    text=advice.render_main(result["main"],items)
    assert "3.300.000" in text and "nhà vệ sinh chung" not in text


@pytest.mark.asyncio
async def test_identical_options_do_not_force_an_invented_advantage():
    first=displayed_rooms()[2]
    items=[deepcopy(first)|{"id":identifier} for identifier in IDS[:4]]
    result,evidence=await evaluate({"listingId":IDS[0],"fields":["amenities.WIFI","roomDetail.maxOccupants"]},listings=items)
    assert all(not item["usefulReasonFields"] for item in evidence["mainListings"])
    assert result["main"]["reasons"] == []
    assert "vì" not in advice.render_main(result["main"],items)


@pytest.mark.asyncio
async def test_boundary_fee_highlight_is_a_sentence_not_a_field_boolean_dump():
    items=displayed_rooms()
    candidate=deepcopy(items[2])|{"id":IDS[4]}
    candidate["pricing"]["amount"]=4100000
    candidate["charges"][0]["includedInRent"]=True
    path="charges.SERVICE_OR_GARBAGE.includedInRent"
    generator=AsyncMock()
    generator.generate_value_advice.return_value={"selectedId":IDS[4],"comparedListingIds":IDS[:4],
        "advantages":[{"field":path,"againstIds":IDS[:4]}],"tradeoffs":[]}
    bounds={"candidateIds":[IDS[4]],"budgetCeiling":4000000,"maxExtraMonthly":500000}
    result=await advice.evaluate("Tìm phòng dưới 4 triệu",{},items,[candidate],bounds,generator,CAT)
    text=advice.render(result,items,bounds)
    assert "tiền thuê đã bao gồm **phí dịch vụ và rác**" in text
    assert "tiền thuê **chưa bao gồm phí dịch vụ và rác**" in text
    assert ": Có" not in text and ": Không" not in text and "đã gồm trong tiền thuê:" not in text
    assert "100.000đ/tháng" in text and "tổng chi phí rẻ hơn" not in text


@pytest.mark.asyncio
async def test_why_uses_natural_sentences_and_drops_fields_shared_by_all_four():
    generator=AsyncMock()
    generator.generate_main_explanation.return_value={"reasons":["roomDetail.restroomType","amenities.WIFI","pricing.managementFeeIncluded"],"tradeoffs":[]}
    text=await explain("Vì sao chọn phòng này?",{"listingId":IDS[2]},displayed_rooms(),{},IDS[:4],CAT,generator)
    assert "có **nhà vệ sinh riêng**" in text and "dùng **nhà vệ sinh chung**" in text
    assert "WiFi" not in text and "phí quản lý" not in text and ": Có" not in text
    assert "\n- " in text


@pytest.mark.asyncio
async def test_native_model_schema_respects_all_private_and_shared_arrangements(monkeypatch):
    response={"candidateAssessments":[],"selectedId":None,"comparedListingIds":IDS[:4],"advantages":[],"tradeoffs":[],
              "mainRecommendation":{"listingId":IDS[2],"fields":["roomDetail.restroomType","pricing.amount"]}}
    calls=mock_http(monkeypatch,[httpx.Response(200,json=completed(json.dumps(response)))])
    await advice.evaluate("Tôi là sinh viên",{},displayed_rooms(),[],{},OpenAIGenerativeClient(settings()),CAT,recommend_main=True)
    schema=json.loads(calls[0].content)["text"]["format"]["schema"]
    assert schema["properties"]["mainRecommendation"]["properties"]["listingId"]["enum"] == [IDS[0], IDS[2], IDS[3]]
    assert "pricing.managementFeeIncluded" not in schema["properties"]["mainRecommendation"]["properties"]["fields"]["items"]["enum"]


@pytest.mark.parametrize("lead", ["- **Gợi ý chọn:**", "**Mình gợi ý bạn cân nhắc**", "### Lựa chọn ưu tiên:"])
def test_model_verdict_cannot_compete_with_the_apps_verified_choice(lead):
    text=f"Mình đối chiếu các phòng như sau:\n\n- **Phòng 1:** rộng 30 m², dùng WC chung.\n- **Phòng 3:** rộng 30 m², có WC riêng.\n{lead} Phòng 2 là lựa chọn tốt nhất.\n  Đây là phần kết luận thêm của mô hình.\n\nBạn còn muốn xem thông tin gì?"
    result=without_managed_choice(text)
    assert "**Phòng 1:** rộng 30 m², dùng WC chung." in result
    assert "**Phòng 3:** rộng 30 m², có WC riêng." in result
    assert "lựa chọn tốt nhất" not in result and "kết luận thêm" not in result
    assert "Bạn còn muốn xem thông tin gì?" in result


def test_reason_bullets_attached_to_an_unverified_model_verdict_are_not_left_orphaned():
    text="Mình gợi ý Phòng 2.\n\n- **Lý do:** WC chung.\n  Phần giải thích tiếp theo.\n\n- **Phòng 3:** có WC riêng."
    result=without_managed_choice(text)
    assert "**Lý do:**" not in result and "Phần giải thích" not in result
    assert "**Phòng 3:** có WC riêng." in result


@pytest.mark.parametrize("field", ["restroomType", "kitchenType", "balconyType", "accessType", "electricMeterType", "waterMeterType"])
@pytest.mark.asyncio
async def test_every_private_or_shared_arrangement_is_compared_without_a_wc_special_case(field):
    items=[deepcopy(displayed_rooms()[2])|{"id":identifier} for identifier in IDS[:4]]
    for index,item in enumerate(items):
        item["roomDetail"][field]="PRIVATE" if index in (1,3) else "SHARED"
    assert advice.main_selection_ids(items,{}) == [IDS[1],IDS[3]]
    path="roomDetail."+field
    result,evidence=await evaluate({"listingId":IDS[1],"fields":[path]},listings=items)
    assert path in evidence["mainListings"][1]["usefulReasonFields"]
    assert path not in evidence["mainListings"][0]["usefulReasonFields"]
    text=advice.render_main(result["main"],items)
    assert "riêng" in text and "chung" not in text


@pytest.mark.parametrize("field", ["kitchenType", "balconyType"])
@pytest.mark.asyncio
async def test_shared_facility_is_not_promoted_even_if_other_rooms_have_no_such_facility(field):
    items=[deepcopy(displayed_rooms()[2])|{"id":identifier} for identifier in IDS[:4]]
    for item in items:
        item["roomDetail"][field]="NONE"
    candidate=deepcopy(items[0])|{"id":IDS[4]}
    candidate["pricing"]["amount"]=4100000
    candidate["roomDetail"][field]="SHARED"
    path="roomDetail."+field
    proposed=advice._facts(candidate,CAT)
    mains={item["id"]:advice._facts(item,CAT) for item in items}
    assert path not in advice._whole_page_advantages(proposed,mains,{})
    assert path not in advice.useful_main_fields(proposed,mains,{})
    generator=AsyncMock()
    generator.generate_main_explanation.return_value={"reasons":[path],"tradeoffs":[]}
    text=await explain("Vì sao?",{"listingId":IDS[4]},[candidate,*items[:3]],{},[IDS[4],*IDS[:3]],CAT,generator)
    assert "Điểm cần cân nhắc" in text and "không phải lợi thế về sự riêng tư" in text
    assert "thuận lợi hơn" not in text


def test_different_private_advantages_and_unknown_data_do_not_get_an_arbitrary_privacy_score():
    items=[deepcopy(displayed_rooms()[2])|{"id":identifier} for identifier in IDS[:2]]
    items[0]["roomDetail"].update(restroomType="PRIVATE",kitchenType="SHARED")
    items[1]["roomDetail"].update(restroomType="SHARED",kitchenType="PRIVATE")
    assert advice.main_selection_ids(items,{}) == IDS[:2], "Each room has a different private facility; no WC-only rule"
    items[1]["roomDetail"].pop("restroomType")
    assert advice.main_selection_ids(items,{}) == IDS[:2], "Unknown must not become shared or absent"
