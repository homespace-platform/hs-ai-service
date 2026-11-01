"""Natural role/context replies must consult the shortlist, not repeat its search table."""
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from homespace_ai.application import renter_guidance as guidance, renter_search_state as rs
from homespace_ai.application.renter_criteria import normalize
from homespace_ai.application.renter_context_intake import read_context
from homespace_ai.clients.generative import GenerationUnavailableError, OpenAIGenerativeClient
from tests.unit.listing_fakes import IDS, c, search
from tests.unit.test_listing_tool_planner import CAT, TODAY, strict
from tests.unit.test_openai_generation import completed, mock_http, settings
from tests.unit.test_renter_profile_advice import profile_setup


def interpreted(persona,quote,groups,changed=()):
    return {"persona":persona,"descriptionQuote":quote,"adviceGroups":groups,"newCriteriaQuotes":list(changed),
            "needs":[],"studyPlace":None,"workPlace":None}


@pytest.mark.parametrize("question,persona", [
    ("tôi là sinh viên thì nên chọn phòng nào?", "STUDENT"),
    ("Mình là người đi làm, nên chọn phòng nào?", "WORKING"),
    ("Tôi muốn thuê cho gia đình thì nên chọn phòng nào?", "FAMILY"),
])
@pytest.mark.asyncio
async def test_advice_tail_does_not_send_a_known_persona_to_the_old_detail_table(question,persona):
    a,state,history=profile_setup()
    before=deepcopy(state)
    assert guidance.profile_only_reply(question)
    result=await a.try_answer(question,history,"exact-user-report",state=state)
    assert result.status=="ANSWERED" and result.listing_ids==IDS[:4]
    assert "| Điểm phù hợp để cân nhắc |" in result.answer and "| Ảnh |" not in result.answer
    assert "quanh phòng |" in result.answer and len(a.tools.nearby_calls)==1
    assert a.planner.calls==0 and not a.tools.value_calls
    saved=result.listing_context["searchState"]
    assert saved["renterProfile"]["persona"]==persona
    for key in ("constraints","location","context","shown"): assert saved[key]==before[key]


@pytest.mark.parametrize("question,persona,quote,groups", [
    ("Mình làm điều dưỡng ca đêm, bạn tư vấn phòng nào hợp với mình?", "WORKING", "Mình làm điều dưỡng ca đêm", ["HEALTHCARE","TRANSPORT","SHOPPING"]),
    ("Tôi đã nghỉ hưu, trong các phòng trên tôi nên chọn phòng nào?", "OTHER", "Tôi đã nghỉ hưu", ["HEALTHCARE","SHOPPING","RECREATION"]),
    ("Mình bán hàng online tại nhà thì nên chọn phòng nào?", "OTHER", "Mình bán hàng online tại nhà", ["TRANSPORT","SHOPPING","FOOD_DRINK"]),
    ("Mình vừa học vừa làm ca tối, nên chọn phòng nào?", "STUDENT", "Mình vừa học vừa làm ca tối", ["EDUCATION","TRANSPORT","FOOD_DRINK"]),
])
@pytest.mark.asyncio
async def test_semantic_context_is_grounded_persisted_and_used_without_a_new_global_search(question,persona,quote,groups):
    a,state,history=profile_setup();before=deepcopy(state)
    assert guidance.contextual_consultation(question,state)
    a.generative_client.generate_renter_context=AsyncMock(return_value=interpreted(persona,quote,groups))
    result=await a.try_answer(question,history,"semantic-profile",state=state)
    assert a.planner.calls==0 and len(a.tools.payloads)==1 and a.tools.payloads[0]["scopeIds"]==IDS[:4]
    assert not a.tools.value_calls and result.listing_ids==IDS[:4]
    assert "| Điểm phù hợp để cân nhắc |" in result.answer and "| Ảnh |" not in result.answer
    profile=result.listing_context["searchState"]["renterProfile"]
    assert profile=={"persona":persona,"description":quote,"adviceGroups":groups}
    assert a.tools.nearby_calls[0][2]==groups
    assert a.generative_client.boundary_calls[-1]["confirmedCriteria"]["renterProfile"]==profile
    for key in ("constraints","location","context","shown"):
        assert result.listing_context["searchState"][key]==before[key]


@pytest.mark.asyncio
async def test_new_hard_criteria_in_a_role_question_still_reach_core_instead_of_being_ignored():
    a,state,history=profile_setup()
    a.generative_client.generate_renter_context=AsyncMock(return_value=interpreted("WORKING","Mình làm điều dưỡng",["TRANSPORT"],
        ["tăng ngân sách tối đa 5 triệu cho 4 người"]))
    a.planner.plan_result=search(c("rent.monthly_amount","lte",5000000),withinDisplayed=True,
        renterProfile={"persona":"WORKING","quote":"Mình làm điều dưỡng","adviceGroups":["TRANSPORT"]},
        context={"occupants":4})
    result=await a.try_answer("Mình làm điều dưỡng, tăng ngân sách tối đa 5 triệu cho 4 người",history,"hard-delta",state=state)
    assert a.tools.payloads[-1]["context"]["occupants"]==4
    assert {"field":"rent.monthly_amount","op":"lte","value":"5000000"} in a.tools.payloads[-1]["where"]["all"]
    assert result.listing_context["searchState"]["renterProfile"]["description"]=="Mình làm điều dưỡng"


def test_unverified_or_negated_context_is_not_saved_and_old_role_metadata_is_cleared_on_a_switch():
    assert guidance.profile_update("Tìm phòng phù hợp",{"persona":"OTHER","quote":"Tôi đã nghỉ hưu"})=={}
    assert guidance.profile_update("Nếu tôi đã nghỉ hưu thì sao?",{"persona":"OTHER","quote":"Nếu tôi đã nghỉ hưu"})=={}
    assert guidance.profile_update("Tôi không phải là sinh viên",{"persona":"STUDENT","quote":"Tôi không phải là sinh viên"})=={}
    profile=guidance.profile_update("Mình làm điều dưỡng",{"persona":"WORKING","quote":"Mình làm điều dưỡng","adviceGroups":["INVENTED"]})
    assert "adviceGroups" not in profile
    state=rs.empty_state()|{"renterProfile":{"persona":"OTHER","description":"Tôi đã nghỉ hưu","adviceGroups":["HEALTHCARE"]}}
    changed=rs.merge(state,{"renterProfile":{"persona":"STUDENT"}},"Tôi là sinh viên")
    assert changed["renterProfile"]=={"persona":"STUDENT"}
    assert rs.sanitize(rs.merge(None,{"renterProfile":profile},""))["renterProfile"]==profile


def test_semantic_role_text_is_not_a_listing_keyword_but_literal_search_remains_available():
    quote="Mình làm điều dưỡng ca đêm"
    args=search(keyword="điều dưỡng",renterProfile={"persona":"WORKING","quote":quote}).arguments
    update=normalize(quote+", bạn tư vấn phòng nào?",args,CAT,None,TODAY)
    assert update["keyword"] is None and update["renterProfile"]["description"]==quote
    assert normalize(quote+", tìm chữ điều dưỡng trong tiêu đề",args,CAT,None,TODAY)["keyword"]=="điều dưỡng"


@pytest.mark.asyncio
async def test_native_context_interpretation_has_no_slots_for_generated_listing_predicates(monkeypatch):
    calls=mock_http(monkeypatch,[httpx.Response(200,json=completed(json.dumps(interpreted("OTHER","Tôi đã nghỉ hưu",["HEALTHCARE"]))))])
    result,changed=await read_context(OpenAIGenerativeClient(settings()),"Tôi đã nghỉ hưu, tôi nên chọn phòng nào?",{})
    payload=json.loads(calls[0].content)
    schema=payload["text"]["format"]["schema"]
    strict(schema)
    assert "adviceGroups" in schema["required"] and "newCriteriaQuotes" in schema["required"]
    assert not ({"constraints","keyword","nearby","context","location"}&set(schema["properties"]))
    assert result["renterProfile"]["persona"]=="OTHER" and not changed and "tools" not in payload


@pytest.mark.parametrize("broken",["ungrounded","unavailable"])
@pytest.mark.asyncio
async def test_context_failure_asks_friendly_clarification_without_inventing_a_persona_or_room_conditions(broken):
    a,state,history=profile_setup()
    if broken=="ungrounded":
        a.generative_client.generate_renter_context=AsyncMock(return_value=interpreted("OTHER","Tôi đã nghỉ hưu",["HEALTHCARE"]))
    else:
        a.generative_client.generate_renter_context=AsyncMock(side_effect=GenerationUnavailableError("model down"))
    result=await a.try_answer("Mình làm điều dưỡng ca đêm thì nên chọn phòng nào?",history,"context-failure",state=state)
    assert result.status=="NEEDS_CLARIFICATION" and "chưa nắm chắc" in result.answer
    assert result.listing_context["listingIds"]==IDS[:4]
    assert not a.tools.payloads and not a.tools.nearby_calls and not a.tools.value_calls and a.planner.calls==0


@pytest.mark.parametrize("malformed", [{"persona":[]}, {"persona":"OTHER","adviceGroups":[{}]}])
@pytest.mark.asyncio
async def test_malformed_context_cannot_crash_the_chat_or_write_unverified_memory(malformed):
    a,state,history=profile_setup()
    a.generative_client.generate_renter_context=AsyncMock(return_value=malformed)
    result=await a.try_answer("Bác đã nghỉ hưu, bác nên chọn phòng nào?",history,"bad-context-shape",state=state)
    assert result.status=="NEEDS_CLARIFICATION" and result.listing_context["searchState"]["renterProfile"]==state["renterProfile"]
    assert not a.tools.payloads and not a.tools.nearby_calls
