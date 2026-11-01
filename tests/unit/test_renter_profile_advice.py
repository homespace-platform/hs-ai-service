"""Persona follow-ups evaluate the existing rooms AND cached outside places, never repeat a search table."""
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from homespace_ai.application import listing_value_advice as value
from homespace_ai.application.nearby_table import _place_cell, facts as nearby_facts
from homespace_ai.application.renter_profile_advice import PERSONA_GROUPS, observed_nearby, render_profile_table, selected_nearby_text
from homespace_ai.clients.generative import GenerationUnavailableError, OpenAIGenerativeClient
from homespace_ai.tools.listings import ListingToolError
from tests.unit.listing_fakes import IDS
from tests.unit.test_nearby_followup_workflow import setup
from tests.unit.test_listing_value_advice import CAT
from tests.unit.test_openai_generation import completed, mock_http, settings


def profile_setup():
    a, state, history = setup()
    for index, identifier in enumerate(IDS[:4]):
        a.tools.details[identifier]["roomDetail"].update(restroomType="PRIVATE" if index in (0, 3) else "SHARED",
            kitchenType="PRIVATE" if index in (0, 2) else "SHARED", maxOccupants=3)
        for group in a.tools.summaries[index]["groups"]:
            group["nearest"] = [{"placeId": IDS[5+n], "name": f"Địa điểm {group['code']} {index+1}-{n+1}",
                                 "distanceMeters": 100 + index*150+n} for n in range(5)]
    a.generative_client.boundary_decision["mainRecommendation"] = {"listingId": IDS[0],
        "fields": ["roomDetail.restroomType", "nearby.SHOPPING.distanceMeters"]}
    a.generative_client.boundary_decision["profileAssessments"] = [
        {"listingId": identifier, "strengths": ["roomDetail.restroomType", "roomDetail.kitchenType"],
         "considerations": ["pricing.amount"]} for identifier in IDS[:4]]
    return a, state, history


@pytest.mark.parametrize("question,persona", [("tôi muốn thuê cho gia đình", "FAMILY"),
    ("tôi là sinh viên", "STUDENT"), ("tôi là người đi làm", "WORKING"), ("tôi làm việc từ xa", "REMOTE")])
@pytest.mark.asyncio
async def test_profile_answer_reads_one_batch_and_renders_selected_room_facts_and_real_nearby_in_one_table(question, persona):
    a, state, history = profile_setup()
    before = deepcopy(state)
    result = await a.try_answer(question, history, "role", state=state)
    assert result.status == "ANSWERED" and result.listing_ids == IDS[:4]
    assert len(a.tools.payloads)==1 and a.tools.payloads[0]["scopeIds"]==IDS[:4]
    assert not a.tools.value_calls and a.planner.calls==0
    assert len(a.tools.nearby_calls)==1
    assert a.tools.nearby_calls[0][:3] == (IDS[:4], 2000, PERSONA_GROUPS[persona])
    assert a.tools.read==IDS[:4] and len(a.generative_client.boundary_calls)==1
    assert "| Điểm phù hợp để cân nhắc |" in result.answer and "| Điểm cần cân nhắc |" in result.answer
    assert "| Ảnh |" not in result.answer and "| Có thể nhận |" not in result.answer
    assert "| Tiện ích trong tòa nhà | WiFi" in result.answer
    for code in PERSONA_GROUPS[persona]:
        assert _place_cell({"name": f"Địa điểm {code} 1-5"}) in result.answer, "All five cached locations survive rendering"
    strengths = next(line for line in result.answer.splitlines() if line.startswith("| Điểm phù hợp"))
    assert "nhà vệ sinh riêng" in strengths and "bếp riêng" in strengths
    assert "vệ sinh chung" not in strengths and "bếp chung" not in strengths
    assert "Mình gợi ý bạn cân nhắc" in result.answer and "Quanh lựa chọn này có" in result.answer
    assert "Địa điểm SHOPPING 1-1" in result.answer
    assert "100 m" not in result.answer and "distanceMeters" not in result.answer
    saved=result.listing_context["searchState"]
    assert saved["renterProfile"]["persona"]==persona
    assert saved["constraints"]==before["constraints"] and saved["context"]==before["context"]
    evidence=a.generative_client.boundary_calls[-1]
    assert evidence["profileComparison"] is True and evidence["candidates"]==[]
    assert "nearby.SHOPPING.distanceMeters" in evidence["factMatrix"]
    assert evidence["observedNearby"][IDS[0]]["groups"]["SHOPPING"]["names"][-1]=="Địa điểm SHOPPING 1-5"
    if persona=="STUDENT":
        assert "nearby.EDUCATION.distanceMeters" not in evidence["factMatrix"]
        assert "Bạn đang học trường nào" in result.answer
    if persona=="WORKING":
        assert "nơi làm việc" in result.answer and "gần công ty" not in result.answer


@pytest.mark.parametrize("mode", ["missing", "partial", "unavailable", "disabled"])
@pytest.mark.asyncio
async def test_incomplete_nearby_keeps_rooms_without_inventing_absence_or_recommending_from_uncovered_distances(mode):
    a,state,history=profile_setup()
    if mode=="missing":
        a.tools.summaries=[]
    elif mode=="partial":
        for group in a.tools.summaries[0]["groups"]:
            group["coverageStatus"]="PARTIAL";group["radiusCoveredMeters"]=500
        a.tools.summaries=a.tools.summaries[:1]
    elif mode=="unavailable":
        a.tools.nearby=AsyncMock(side_effect=ListingToolError("HTTP 503"))
    else:
        a.settings.nearby_query_enabled=False
    result=await a.try_answer("tôi muốn thuê cho gia đình",history,"fallback",state=state)
    assert result.status=="ANSWERED" and result.listing_ids==IDS[:4]
    assert "| Điểm phù hợp" in result.answer and "| Tiêu chí |" not in result.answer
    assert "khu vực không có" not in result.answer
    assert "nearby.SHOPPING.distanceMeters" not in a.generative_client.boundary_calls[-1]["factMatrix"]
    if mode=="partial":
        assert "mainRecommendation" not in result.listing_context
        assert "chưa có đủ cơ sở để chọn" in result.answer
        assert a.generative_client.boundary_calls[-1]["observedNearby"][IDS[0]]["groups"]["SHOPPING"]["names"]
    else:
        assert "Quanh lựa chọn này có" not in result.answer
    if mode=="disabled": assert not a.tools.nearby_calls
    if mode in {"missing","unavailable","disabled"}: assert result.data_sources==[]


@pytest.mark.asyncio
async def test_model_failure_keeps_the_focused_table_and_cached_places_without_forcing_a_choice():
    a,state,history=profile_setup()
    a.generative_client.generate_value_advice=AsyncMock(side_effect=GenerationUnavailableError("timeout"))
    result=await a.try_answer("tôi là sinh viên",history,"model-down",state=state)
    assert result.status=="ANSWERED" and result.listing_ids==IDS[:4]
    assert "Địa điểm SHOPPING 1-5" in result.answer
    assert "chưa có đủ cơ sở để chọn" in result.answer and "mainRecommendation" not in result.listing_context
    assert not a.tools.value_calls


@pytest.mark.asyncio
async def test_rooms_that_no_longer_meet_old_constraints_are_excluded_before_any_nearby_request():
    a,state,history=profile_setup()
    a.tools.ids=IDS[1:4]
    a.generative_client.boundary_decision["mainRecommendation"]={"listingId":IDS[1],"fields":[]}
    a.generative_client.boundary_decision["profileAssessments"]=a.generative_client.boundary_decision["profileAssessments"][1:]
    result=await a.try_answer("tôi muốn thuê cho gia đình",history,"stale",state=state)
    assert result.listing_ids==IDS[1:4] and a.tools.nearby_calls[0][0]==IDS[1:4]
    assert f"[Phòng 2](/rent/{IDS[1]}" in result.answer and f"[Phòng 1](/rent/{IDS[0]}" not in result.answer
    a.tools.ids=[];a.tools.nearby_calls.clear()
    empty=await a.try_answer("tôi muốn thuê cho gia đình",history,"no-rooms",state=state)
    assert empty.status=="NO_RESULTS" and not a.tools.nearby_calls and not a.tools.value_calls


@pytest.mark.asyncio
async def test_native_profile_schema_requires_each_room_and_only_allows_verified_field_references(monkeypatch):
    a,state,_=profile_setup()
    state["renterProfile"]={"persona":"FAMILY"}
    listings=[a.tools.details[i] for i in IDS[:4]]
    response={"candidateAssessments":[],"selectedId":None,"comparedListingIds":IDS[:4],"advantages":[],"tradeoffs":[],
        "mainRecommendation":{"listingId":IDS[0],"fields":["nearby.SHOPPING.distanceMeters"]},
        "profileAssessments":a.generative_client.boundary_decision["profileAssessments"]}
    calls=mock_http(monkeypatch,[httpx.Response(200,json=completed(json.dumps(response)))])
    advice=await value.evaluate("Tôi muốn thuê cho gia đình",state,listings,[],{},OpenAIGenerativeClient(settings()),CAT,
        recommend_main=True,nearby_facts=nearby_facts(a.tools.summaries,radius=2000,profile=state["renterProfile"]),profile_comparison=True)
    schema=json.loads(calls[0].content)["text"]["format"]["schema"]
    rows=schema["properties"]["profileAssessments"]
    assert rows["minItems"]==rows["maxItems"]==4 and rows["items"]["additionalProperties"] is False
    assert "profileAssessments" in schema["required"]
    assert rows["items"]["properties"]["listingId"]["enum"]==IDS[:4]
    assert all(not path.startswith("nearby.") for path in rows["items"]["properties"]["strengths"]["items"]["enum"])
    assert advice["main"]["reasons"][0]["field"]=="nearby.SHOPPING.distanceMeters"
    assert len(advice["profileAssessments"])==4


@pytest.mark.parametrize("invalid", ["unknown_field", "unprovided_fact", "duplicate_room", "opposite_claims"])
@pytest.mark.asyncio
async def test_unverified_profile_claims_never_reach_customer_text(invalid):
    a,state,history=profile_setup()
    rows=a.generative_client.boundary_decision["profileAssessments"]
    if invalid=="unknown_field": rows[0]["strengths"]=["invented.familyFriendly"]
    elif invalid=="unprovided_fact": rows[0]["considerations"]=["roomDetail.unprovided"]
    elif invalid=="duplicate_room": rows[0]["listingId"]=rows[1]["listingId"]
    else: rows[0]["considerations"]=rows[0]["strengths"]
    result=await a.try_answer("tôi muốn thuê cho gia đình",history,"unverified",state=state)
    assert result.status=="ANSWERED" and result.listing_ids==IDS[:4]
    assert "invented" not in result.answer and "unprovided" not in result.answer
    assert "mainRecommendation" not in result.listing_context and "chưa có đủ cơ sở" in result.answer
    assert "Địa điểm SHOPPING 1-5" in result.answer


def test_profile_table_escapes_source_labels_and_keeps_targets_separate_from_building_amenities():
    a,_,_=profile_setup()
    listing=a.tools.details[IDS[0]]
    listing["amenities"]=[{"code":"PRIVATE_INTERNAL_CODE"}, {"name":"<script>alert(1)</script> | [link](https://bad.example)"}]
    summary=a.tools.summaries[0]
    summary["target"]={"name":"Trường đại học đã chọn"}
    summary["groups"][0]["nearest"][0]["name"]="<img src=x> | [link](https://bad.example)"
    text=render_profile_table([listing],IDS[:4],[],[summary],PERSONA_GROUPS["FAMILY"],"FAMILY")
    assert "<script>" not in text and "<img" not in text and "[link](" not in text
    assert "PRIVATE_INTERNAL_CODE" not in text and "| Địa điểm bạn quan tâm | Trường đại học đã chọn |" in text
    assert "&lt;script&gt;" in text and "&lt;img" in text and "<br>" in text


def test_observed_locations_include_partial_coverage_but_reject_unknown_or_outside_positions():
    a,_,_=profile_setup()
    summary=a.tools.summaries[0]
    group=next(g for g in summary["groups"] if g["code"]=="SHOPPING")
    group.update(coverageStatus="PARTIAL",radiusCoveredMeters=500)
    group["nearest"]=[{"name":"Cửa hàng đã ghi nhận","distanceMeters":1900},
                      {"name":"Ngoài phạm vi","distanceMeters":2001},
                      {"name":"Thiếu tọa độ"}, {"name":"Không hợp lệ","distanceMeters":float('nan')},
                      {"name":"Khoảng cách không hợp lệ","distanceMeters":True}]
    summary["target"]={"name":"Trường đã chọn","withinRadius":True,"distanceMeters":1700}
    observed=observed_nearby([summary],["SHOPPING"],2000)
    assert observed[IDS[0]]["groups"]["SHOPPING"]=={"names":["Cửa hàng đã ghi nhận"],"coverageStatus":"PARTIAL"}
    assert "nearby.SHOPPING.distanceMeters" not in nearby_facts([summary],radius=2000)[IDS[0]]
    conclusion=selected_nearby_text(observed,IDS[0],["SHOPPING"],"FAMILY")
    assert "Trường đã chọn" in conclusion and "Cửa hàng đã ghi nhận" in conclusion
    assert "1900" not in conclusion and "gần nhất" not in conclusion


@pytest.mark.parametrize("path,raw,shown,expected", [
    ("roomDetail.hasMezzanine",True,"Có","có **gác lửng**"),
    ("roomDetail.furnishingStatus","PARTIALLY_FURNISHED","Một phần nội thất","được bàn giao **một phần nội thất**"),
    ("roomDetail.furnishingStatus","FULLY_FURNISHED","Đầy đủ nội thất","được bàn giao **đầy đủ nội thất**"),
    ("maxMotorbikeCount",2,"2 xe","có chỗ gửi tối đa **2 xe máy**"),
    ("maxCarCount",0,"0 xe","không nhận gửi ô tô"),
])
def test_profile_assessment_phrases_use_natural_words_instead_of_repeated_field_labels(path,raw,shown,expected):
    assert value.reason_text({"field":path,"value":raw,"display":shown,"label":"Có gác lửng (phòng trọ)"})==expected
