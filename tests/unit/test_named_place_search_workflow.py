"""Named POI searches reuse typed listing search, intake and durable pagination."""
import pytest
from httpx import Response
from types import SimpleNamespace
from homespace_ai.api.v1.agent_ask import stored_answer
from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.tools.listings import ListingTools
from tests.unit.listing_fakes import IDS, Conversation, c, make_assistant, search
from tests.unit.test_listing_tool_planner import plan_with, call, config

TARGET={"placeId": "11111111-1111-1111-1111-111111111111",
        "name": "Trường Đại Học Công Nghiệp Thành Phố Hồ Chí Minh", "address": "12 Đường Nguyễn Văn Bảo, Hạnh Thông, Hồ Chí Minh"}

@pytest.mark.asyncio
async def test_fresh_landmark_search_never_offers_a_shortlist_only_poi_tool(monkeypatch):
    result,payload=await plan_with(monkeypatch,call("search_listings",search(place="Đại học Công nghiệp HCM").arguments),
        question="Tôi cần tìm phòng trọ gần Đại học Công nghiệp HCM",settings=config())
    assert result.name=="search_listings"
    assert "get_listing_nearby_places" not in {t["name"] for t in payload["tools"]}

@pytest.mark.asyncio
async def test_place_search_intake_retains_target_then_renders_a_regular_table_and_next_page():
    query="Đại học công nghiệp HCM"
    a=make_assistant(search(c("category","eq","ROOM"),place=query),IDS[:7])
    a._require_search_basics=True
    a.tools.places={query:[TARGET]}
    chat=Conversation(a)
    result=await chat.ask("Tôi cần tìm phòng trọ gần Đại học công nghiệp HCM")
    assert not a.tools.payloads and result.status=="NEEDS_CLARIFICATION"
    assert result.listing_context["searchState"]["target"]["placeId"]==TARGET["placeId"]
    a.planner.plan_result=search(c("rent.monthly_amount","lt",4000000),context={"occupants":3})
    result=await chat.ask("Cho 3 người, tiền thuê dưới 4 triệu, cho mình xem 4 phòng")
    assert "| Tiêu chí |" in result.answer and result.listing_ids==IDS[:4]
    assert a.tools.payloads[-1]["nearbyAll"]==[{"placeId":TARGET["placeId"],"radiusMeters":2000}]
    assert a.tools.payloads[-1]["context"]["occupants"]==3
    assert not a.tools.nearby_calls and "keyword" not in a.tools.payloads[-1]
    a.planner.plan_result=search(moreResults=True)
    next_result=await chat.ask("Xem thêm")
    assert next_result.listing_ids==IDS[4:7]
    assert a.tools.payloads[-1]["excludeIds"]==IDS[:4]
    assert a.tools.payloads[-1]["nearbyAll"][0]["placeId"]==TARGET["placeId"]
    assert "| Tiêu chí |" in next_result.answer

@pytest.mark.asyncio
async def test_name_and_full_address_is_sent_intact_to_core_without_alias_substitution():
    calls=[]
    class Gateway:
        async def request(self,method,path,**kwargs):
            calls.append((method,path,kwargs["params"]))
            return Response(200,json={"result":{"places":[{"id":TARGET["placeId"],"name":TARGET["name"],"address":TARGET["address"]}]}})
    tools=ListingTools(Gateway(),SimpleNamespace())
    query=TARGET["name"]+" — "+TARGET["address"]
    assert await tools.resolve_place(query)==[TARGET]
    assert calls[0][2]=={"keyword":query}

def test_target_clarification_is_not_replaced_by_a_generic_nearby_history_message():
    state=rs.empty_state()
    state["pending"]={"type":"TARGET","candidates":[TARGET],"question":"Bạn muốn tìm ở cơ sở nào?"}
    answer=AskResult(answer="Bạn muốn tìm ở cơ sở nào? 12 Đường Nguyễn Văn Bảo",status="NEEDS_CLARIFICATION",citations=[],request_id="target",
        listing_context={"searchState":state},data_sources=["CORE_NEARBY"])
    assert stored_answer(answer)==answer.answer
