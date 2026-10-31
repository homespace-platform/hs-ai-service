"""Regression: 'tôi muốn thuê cho gia đình' is profile context, never a title keyword."""
from copy import deepcopy

import pytest

from homespace_ai.application import renter_guidance as guidance, renter_search_state as rs
from homespace_ai.application.conversation_listing_memory import conversation_memory
from homespace_ai.application.listing_context import current_scope
from homespace_ai.application.renter_criteria import normalize
from tests.unit.listing_fakes import IDS, Conversation, c, search
from tests.unit.test_renter_guidance import assistant, complete_plan
from tests.unit.test_listing_value_advice import CAT
from tests.unit.test_listing_tool_planner import TODAY


@pytest.mark.asyncio
@pytest.mark.parametrize("question,keyword,persona", [
    ("tôi muốn thuê cho gia đình", "gia đình", "FAMILY"),
    ("mình cần thuê cho gia đình", "gia đình", "FAMILY"),
    ("tôi muốn thuê, tôi là sinh viên", "sinh viên", "STUDENT"),
    ("mình là người đi làm", "người đi làm", "WORKING"),
    ("gia đình", "gia đình", "FAMILY"),
    ("sinh viên", "sinh viên", "STUDENT"),
])
async def test_profile_answer_keeps_the_same_shortlist_filters_and_pagination(question,keyword,persona):
    a=assistant()
    a.planner.plan_result=complete_plan()
    chat=Conversation(a)
    first=await chat.ask("Tìm phòng trọ phường Gò Vấp, HCM dưới 3,5 triệu cho 2 người")
    before=deepcopy(chat.state())
    searches,boundaries,plans=len(a.tools.payloads),len(a.tools.value_calls),a.planner.calls
    a.tools.read.clear()
    # Reproduce the actual bad native plan: a persona keyword, otherwise unchanged criteria.
    a.planner.plan_result=search(keyword=keyword)
    result=await chat.ask(question)
    assert result.status=="ANSWERED" and result.listing_ids==first.listing_ids==IDS[:4]
    assert a.tools.read==IDS[:4] and len(a.tools.payloads)==searches
    assert len(a.tools.value_calls)==boundaries and not a.tools.nearby_calls
    assert a.planner.calls==plans, "A standalone answer to our purpose question cannot invent search requirements"
    state=result.listing_context["searchState"]
    assert state["renterProfile"]["persona"]==persona and state["keyword"] is None
    for key in ("location","context","constraints","shown","boundaryArea"):
        assert state[key]==before[key]
    assert a.generative_client.evidence["confirmedCriteria"]["renterProfile"]["persona"]==persona


@pytest.mark.parametrize("question", [
    "Tôi muốn thuê cho gia đình 4 người", "Tôi là sinh viên, ngân sách tối đa 5 triệu",
    "Tôi muốn thuê cho gia đình ở An Nhơn", "Tôi muốn thuê cho gia đình, có bếp riêng",
    "Tôi muốn thuê cho gia đình, ưu tiên phòng rộng", "Tôi muốn thuê cho gia đình, xem thêm",
    "Tôi muốn thuê cho gia đình, tìm tin có chữ gia đình trong tiêu đề",
    "Tôi muốn thuê căn hộ cho gia đình", "Tôi cần tìm nhà nguyên căn cho gia đình",
    "Tôi không phải sinh viên", "Nếu tôi là sinh viên thì sao?",
])
def test_profile_only_shortcut_never_discards_other_customer_instructions(question):
    assert not guidance.profile_only_reply(question)


@pytest.mark.asyncio
async def test_profile_plus_a_real_requirement_change_is_not_mistaken_for_a_profile_only_answer():
    a=assistant();a.planner.plan_result=complete_plan();chat=Conversation(a)
    await chat.ask("Tìm phòng trọ phường Gò Vấp, HCM dưới 3,5 triệu cho 2 người")
    count=len(a.tools.payloads)
    a.planner.plan_result=search(c("rent.monthly_amount","lte",5000000),keyword="gia đình",context={"occupants":4})
    result=await chat.ask("Tôi muốn thuê cho gia đình 4 người, nâng ngân sách tối đa 5 triệu")
    assert len(a.tools.payloads)==count+1
    payload=a.tools.payloads[-1]
    assert payload["context"]["occupants"]==4 and "keyword" not in payload
    assert {"field":"rent.monthly_amount","op":"lte","value":"5000000"} in payload["where"]["all"]
    assert result.listing_context["searchState"]["renterProfile"]["persona"]=="FAMILY"


@pytest.mark.asyncio
async def test_profile_plus_show_more_still_fetches_the_next_page_instead_of_repeating_the_shortlist():
    a=assistant();a.planner.plan_result=complete_plan();chat=Conversation(a)
    await chat.ask("Tìm phòng trọ phường Gò Vấp, HCM dưới 3,5 triệu cho 2 người")
    a.planner.plan_result=search(moreResults=True,keyword="gia đình")
    result=await chat.ask("Tôi muốn thuê cho gia đình, cho xem thêm các phòng khác")
    assert result.listing_ids==IDS[4:8]
    assert a.tools.payloads[-1]["excludeIds"]==IDS[:4]
    assert "keyword" not in a.tools.payloads[-1]


@pytest.mark.parametrize("question", [
    "Tôi muốn thuê cho gia đình, tìm tin có chữ gia đình trong tiêu đề",
    "Tôi muốn thuê cho gia đình, tìm chữ gia đình trong tiêu đề",
])
def test_explicit_literal_keyword_search_is_not_erased_by_a_profile_description(question):
    update=normalize(question,search(keyword="gia đình").arguments,CAT,None,TODAY)
    assert update["keyword"]=="gia đình" and not guidance.profile_keyword("gia đình",question)


def broken_conversation():
    good=rs.merge(None,{"constraints":[c("category","eq","ROOM"),c("rent.monthly_amount","lt",4000000)],
                        "context":{"occupants":3},"location":{"cityId":"12","wardId":"24588"},"sort":"newest"},"")
    good["revision"]=1;good=rs.record_shown(good,IDS[:4],more=False)
    bad=rs.merge(good,{"keyword":"gia đình","renterProfile":{"persona":"FAMILY"}},"tôi muốn thuê cho gia đình")
    bad["revision"]=2;bad["pending"]={"type":"EMPTY_RESULT","question":"Đổi điều kiện nào?","fields":[],"candidates":[],"radiusMeters":None,"proposed":None}
    messages=[{"role":"user","content":"Tìm phòng trọ Gò Vấp cho 3 người dưới 4 triệu"},
              {"role":"assistant","status":"ANSWERED","content":"4 phòng","listingContext":rs.listing_context(IDS[:4],good)},
              {"role":"user","content":"tôi muốn thuê cho gia đình"},
              {"role":"assistant","status":"NO_RESULTS","content":"Không có tin","listingContext":rs.listing_context([],bad)}]
    return {"messages":messages,"renterSearchState":bad},good


def test_existing_broken_chat_is_repaired_from_full_history_without_mutating_the_transcript():
    conversation,good=broken_conversation();original=deepcopy(conversation)
    memory=conversation_memory(conversation)
    assert memory.repaired and memory.state["keyword"] is None and memory.state["pending"] is None
    assert memory.state["renterProfile"]=={"persona":"FAMILY"}
    assert memory.state["revision"]==3, "The next answer must pass the stored optimistic revision guard"
    assert current_scope(memory.history)==IDS[:4]
    assert rs.fingerprint(memory.state)==rs.fingerprint(good)
    assert conversation==original


def test_memory_recovers_criteria_and_reference_ids_older_than_the_raw_prompt_window():
    conversation,good=broken_conversation()
    for n in range(30):
        conversation["messages"] += [{"role":"user","content":f"Tôi hỏi thêm lượt {n}"},
            {"role":"assistant","status":"NO_RESULTS","content":"Chưa có tin","listingContext":rs.listing_context([],conversation["renterSearchState"])}]
    memory=conversation_memory(conversation)
    assert len(memory.history)==8 and memory.repaired
    assert current_scope(memory.history)==IDS[:4] and memory.state["context"]=={"occupants":3}
    assert memory.state["constraints"]==good["constraints"]
    assert "giá" not in str(memory.state["shown"]), "Memory stores IDs, never listing facts"


@pytest.mark.asyncio
async def test_a_reply_in_the_same_previously_broken_chat_works_without_starting_a_new_conversation():
    conversation,_=broken_conversation();memory=conversation_memory(conversation)
    a=assistant();a.planner.plan_result=search(keyword="gia đình")
    result=await a.try_answer("tôi muốn thuê cho gia đình",memory.history,"recovered",state=memory.state)
    assert result.status=="ANSWERED" and result.listing_ids==IDS[:4]
    assert not a.tools.payloads and not a.tools.value_calls
    assert result.listing_context["searchState"]["keyword"] is None


def test_literal_keyword_provenance_and_unrelated_changed_criteria_are_respected():
    conversation,_=broken_conversation()
    conversation["messages"][2]["content"]="Tôi muốn thuê cho gia đình, tìm tin có chữ gia đình trong tiêu đề"
    memory=conversation_memory(conversation)
    assert not memory.repaired and memory.state["keyword"]=="gia đình" and current_scope(memory.history)==[]
    conversation["messages"][2]["content"]="tôi muốn thuê cho gia đình"
    conversation["renterSearchState"]["context"]["occupants"]=5
    memory=conversation_memory(conversation)
    assert memory.repaired and memory.state["context"]["occupants"]==5
    assert current_scope(memory.history)==[], "A former 3-person shortlist cannot silently stand in for a 5-person search"
