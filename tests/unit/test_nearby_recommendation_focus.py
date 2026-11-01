"""Recommendations and explanations must answer the current topic, not unrelated room perks."""
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from homespace_ai.application import listing_value_advice as value
from homespace_ai.application.listing_recommendation_explanation import explain
from homespace_ai.application.nearby_table import facts as nearby_facts
from homespace_ai.clients.generative import OpenAIGenerativeClient
from tests.unit.listing_fakes import IDS
from tests.unit.test_main_recommendation_explanation import CAT, generator, shortlist
from tests.unit.test_nearby_followup_workflow import setup
from tests.unit.test_openai_generation import completed, mock_http, settings
from tests.unit.test_renter_context_consultation import interpreted


@pytest.mark.asyncio
async def test_reported_nearby_question_cannot_recommend_a_private_kitchen_or_internet_even_if_model_insists():
    a,state,history=setup()
    a.tools.details[IDS[0]]["roomDetail"]["kitchenType"]="PRIVATE"
    for identifier in IDS[1:4]: a.tools.details[identifier]["roomDetail"]["kitchenType"]="SHARED"
    a.generative_client.boundary_decision["mainRecommendation"]={"listingId":IDS[0],"fields":["roomDetail.kitchenType","charges.INTERNET.includedInRent"]}
    result=await a.try_answer("Các phòng trên có những tiện ích nào xung quanh, nên chọn phòng nào?",history,"reported",state=state)
    assert "| Tiện ích xung quanh |" in result.answer and result.listing_ids==IDS[:4]
    assert "mainRecommendation" not in result.listing_context and "chưa có đủ khác biệt" in result.answer
    assert "bếp riêng" not in result.answer and "Internet" not in result.answer and "diện tích" not in result.answer
    evidence=a.generative_client.boundary_calls[-1]
    assert evidence["recommendationFocus"]=="NEARBY" and all(path.startswith("nearby.") for path in evidence["factMatrix"])
    assert len(a.tools.nearby_calls)==1 and not a.tools.value_calls


@pytest.mark.parametrize("mode",["partial","same","missing"])
@pytest.mark.asyncio
async def test_inconclusive_location_evidence_does_not_force_a_general_room_recommendation(mode):
    a,state,history=setup()
    for summary in a.tools.summaries:
        for group in summary["groups"]:
            if mode=="partial": group.update(coverageStatus="PARTIAL",radiusCoveredMeters=500)
            elif mode=="same": group["nearest"][0]["distanceMeters"]=100
            else: group["nearest"]=[]
    result=await a.try_answer("Các phòng trên có những tiện ích nào xung quanh, nên chọn phòng nào?",history,"inconclusive",state=state)
    assert result.status=="ANSWERED" and result.listing_ids==IDS[:4]
    assert "mainRecommendation" not in result.listing_context and not a.generative_client.boundary_calls
    assert "không có tiện ích" not in result.answer and "ưu tiên gần" in result.answer
    if mode=="partial": assert "Địa điểm SHOPPING" in result.answer
    history.append({"role":"assistant","content":result.answer,"listingContext":result.listing_context})
    why=await a.try_answer("Vì sao bạn ưu tiên phòng được chọn?",history,"why-no-choice",state=state)
    assert why.status=="NEEDS_CLARIFICATION" and "chưa ưu tiên phòng nào" in why.answer


@pytest.mark.asyncio
async def test_location_comparison_is_independent_of_default_room_privacy_and_stores_only_references():
    a,state,history=setup()
    for index,identifier in enumerate(IDS[:4]):
        a.tools.details[identifier]["roomDetail"]["restroomType"]="SHARED" if index==0 else "PRIVATE"
    result=await a.try_answer("Các phòng trên có những tiện ích nào xung quanh, nên chọn phòng nào?",history,"location-not-wc",state=state)
    assert result.listing_context["mainRecommendation"]=={"listingId":IDS[0],"fields":["nearby.SHOPPING.distanceMeters"],"focus":"NEARBY"}
    assert "Địa điểm SHOPPING" in result.answer and "nhà vệ sinh" not in result.answer
    assert "Địa điểm" not in str(result.listing_context)
    history.extend([{"role":"user","content":"Các phòng trên có những tiện ích nào xung quanh?"},
        {"role":"assistant","content":result.answer,"listingContext":result.listing_context}])
    explained=await a.try_answer("Vì sao bạn ưu tiên phòng được chọn?",history,"why-location",state=state)
    assert "| Tiêu chí đối chiếu |" in explained.answer and "Địa điểm SHOPPING" in explained.answer
    assert "nhà vệ sinh" not in explained.answer and "Giá thuê" not in explained.answer and "3000000" not in explained.answer
    assert "100 m" not in explained.answer and "distanceMeters" not in explained.answer and "\n- " not in explained.answer
    assert len(a.tools.nearby_calls)==2 and not a.tools.value_calls


@pytest.mark.asyncio
async def test_why_keeps_the_original_reason_rows_before_expanding_without_a_wall_of_text():
    items=shortlist()
    for index,item in enumerate(items):
        item["roomDetail"].update(kitchenType="PRIVATE" if index==0 else "SHARED",hasMezzanine=index==0)
    model=generator(["roomDetail.hasMezzanine","areaM2"],["pricing.amount"])
    text=await explain("Vì sao bạn ưu tiên phòng được chọn?",{"listingId":IDS[0],"fields":["roomDetail.kitchenType"]},items,{},IDS[:4],CAT,model)
    rows=[line for line in text.splitlines() if line.startswith("| ")]
    assert len(rows)<=7 and "\n- " not in text
    assert "Bếp" in rows[2] and "Riêng" in rows[2] and "Chung" in rows[2]
    assert "37 m²" in text and "33 m²" in text and "3.300.000 đ/tháng" in text
    assert "đang chọn" in rows[0] and all(f"/rent/{i}" in rows[0] for i in IDS[:4])


@pytest.mark.asyncio
async def test_old_bad_nearby_rationale_is_not_replayed_as_kitchen_advice_after_restart():
    a,state,history=setup()
    history=[{"role":"user","content":"Các phòng trên có những tiện ích nào xung quanh?"},
        {"role":"assistant","content":"Bảng cũ", "listingContext":a.context(IDS[:4],state)|{
            "nearby":{"radiusMeters":2000,"groups":["SHOPPING"]},
            "mainRecommendation":{"listingId":IDS[0],"fields":["roomDetail.kitchenType"]}}}]
    result=await a.try_answer("Vì sao bạn ưu tiên phòng được chọn?",history,"legacy-bad-nearby",state=state)
    assert "bếp riêng" not in result.answer and "Internet" not in result.answer
    assert len(a.tools.nearby_calls)==1 and result.listing_context["mainRecommendation"]["focus"]=="NEARBY"


@pytest.mark.asyncio
async def test_native_nearby_judge_only_accepts_poi_proofs_and_allows_abstaining(monkeypatch):
    a,state,_=setup();items=[a.tools.details[i] for i in IDS[:4]]
    response={"candidateAssessments":[],"selectedId":None,"comparedListingIds":IDS[:4],"advantages":[],"tradeoffs":[],
        "mainRecommendation":{"listingId":None,"fields":[]}}
    calls=mock_http(monkeypatch,[httpx.Response(200,json=completed(json.dumps(response)))])
    result=await value.evaluate("Các phòng trên có tiện ích nào xung quanh?",state,items,[],{},OpenAIGenerativeClient(settings()),CAT,
        recommend_main=True,nearby_facts=nearby_facts(a.tools.summaries,radius=2000),recommendation_focus="NEARBY")
    schema=json.loads(calls[0].content)["text"]["format"]["schema"]["properties"]["mainRecommendation"]["properties"]
    assert None in schema["listingId"]["enum"] and all(p.startswith("nearby.") for p in schema["fields"]["items"]["enum"])
    assert result["main"] is None and result["boundary"] is None


@pytest.mark.asyncio
async def test_native_why_location_schema_excludes_room_fields_and_keeps_the_table_topic(monkeypatch):
    a,state,_=setup();items=[a.tools.details[i] for i in IDS[:4]]
    calls=mock_http(monkeypatch,[httpx.Response(200,json=completed(json.dumps({"reasons":["nearby.SHOPPING.distanceMeters"],"tradeoffs":[]})))])
    text=await explain("Vì sao?",{"listingId":IDS[0],"fields":["nearby.SHOPPING.distanceMeters"],"focus":"NEARBY"},
        items,state,IDS[:4],CAT,OpenAIGenerativeClient(settings()),nearby_facts(a.tools.summaries,radius=2000))
    schema=json.loads(calls[0].content)["text"]["format"]["schema"]
    assert all(p.startswith("nearby.") for p in schema["properties"]["reasons"]["items"]["enum"])
    assert "| Tiêu chí đối chiếu |" in text and "100 m" not in text and "Nhà vệ sinh" not in text


@pytest.mark.asyncio
async def test_a_new_persona_in_a_nearby_question_still_answers_nearby_instead_of_recommending_a_kitchen():
    a,state,history=setup()
    a.generative_client.generate_renter_context=AsyncMock(return_value=interpreted("STUDENT","Mình là sinh viên",["EDUCATION","TRANSPORT","SHOPPING"]))
    result=await a.try_answer("Mình là sinh viên, các phòng trên có những tiện ích nào xung quanh?",history,"student-nearby",state=state)
    assert "| Tiện ích xung quanh |" in result.answer and "Điểm phù hợp để cân nhắc" not in result.answer
    assert not a.generative_client.boundary_calls
    assert result.listing_context["searchState"]["renterProfile"]["persona"] == "STUDENT"
    assert "bếp" not in result.answer and "Internet" not in result.answer and "đúng trường nào" not in result.answer
