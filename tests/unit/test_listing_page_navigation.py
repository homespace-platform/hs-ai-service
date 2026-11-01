"""A displayed shortlist is a navigable sequence, not just a cumulative exclusion set."""
from copy import deepcopy

import pytest

from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.conversation_listing_memory import conversation_memory
from homespace_ai.application.listing_context import current_scope
from homespace_ai.application.listing_page_navigation import intent, recover
from tests.unit.listing_fakes import IDS, Conversation, search
from tests.unit.test_renter_guidance import assistant, complete_plan


async def two_pages(size=6):
    a=assistant();a.tools.ids=IDS[:size];a.planner.plan_result=complete_plan()
    chat=Conversation(a)
    first=await chat.ask("Tìm phòng trọ phường Gò Vấp, HCM dưới 3,5 triệu cho 2 người")
    a.planner.plan_result=search(moreResults=True)
    second=await chat.ask("Xem thêm")
    return a,chat,first,second


@pytest.mark.asyncio
async def test_reported_six_rooms_next_two_then_back_returns_the_original_four_with_no_new_search_plan():
    a,chat,first,second=await two_pages()
    assert first.listing_ids==IDS[:4] and second.listing_ids==IDS[4:6]
    before=deepcopy(chat.state());plans=a.planner.calls;boundaries=len(a.tools.value_calls)
    a.tools.read.clear()
    previous=await chat.ask("Quay lại")
    assert previous.status=="ANSWERED" and previous.listing_ids==IDS[:4]
    assert a.tools.read==IDS[:4] and a.planner.calls==plans and len(a.tools.value_calls)==boundaries
    assert a.tools.payloads[-1]["scopeIds"]==IDS[:4] and not a.tools.payloads[-1].get("excludeIds")
    assert "| Tiêu chí |" in previous.answer and "| Ảnh |" in previous.answer and "hết các tin" not in previous.answer
    state=chat.state()
    assert state["pageHistory"]["index"]==0 and state["pageHistory"]["pages"]==[IDS[:4],IDS[4:6]]
    for key in ("location","constraints","context","shown","boundaryArea","renterProfile"):
        assert state[key]==before[key]
    a.tools.read.clear()
    next_page=await chat.ask("Xem thêm")
    assert next_page.listing_ids==IDS[4:6] and a.tools.read==IDS[4:6] and a.planner.calls==plans
    assert chat.state()["pageHistory"]["index"]==1


@pytest.mark.asyncio
async def test_end_of_results_does_not_clear_current_selection_or_make_back_impossible():
    a,chat,_,_=await two_pages()
    ended=await chat.ask("Xem thêm")
    assert ended.status=="ANSWERED" and ended.listing_context["listingIds"]==IDS[4:6]
    assert chat.state()["pending"] is None and current_scope(chat.messages)==IDS[4:6]
    assert "Quay lại" in ended.suggested_questions
    assert (await chat.ask("Quay lại")).listing_ids==IDS[:4]
    assert (await chat.ask("Xem thêm")).listing_ids==IDS[4:6]
    assert (await chat.ask("Xem thêm")).status=="ANSWERED"


@pytest.mark.asyncio
async def test_first_page_back_and_forward_remain_consistent_and_do_not_call_core_when_already_first():
    a,chat,_,_=await two_pages()
    await chat.ask("Quay lại")
    calls=len(a.tools.payloads);plans=a.planner.calls
    beginning=await chat.ask("Trang trước")
    assert "đang ở nhóm phòng đầu tiên" in beginning.answer
    assert len(a.tools.payloads)==calls and a.planner.calls==plans
    assert (await chat.ask("Trang tiếp theo")).listing_ids==IDS[4:6]
    assert (await chat.ask("Xem lại 4 phòng ban đầu")).listing_ids==IDS[:4]


@pytest.mark.asyncio
async def test_multiple_back_steps_and_resume_keep_the_actual_page_boundaries():
    a,chat,_,_=await two_pages(10)
    assert (await chat.ask("Xem thêm")).listing_ids==IDS[8:10]
    assert chat.state()["pageHistory"]["pages"]==[IDS[:4],IDS[4:8],IDS[8:10]]
    assert (await chat.ask("Quay lại 2 trang")).listing_ids==IDS[:4]
    memory=conversation_memory({"messages":chat.messages,"renterSearchState":chat.state()})
    assert memory.state["pageHistory"]["index"]==0
    assert (await a.try_answer("Xem thêm",memory.history,"resume",state=memory.state)).listing_ids==IDS[4:8]
    assert (await chat.ask("Xem lại nhóm phòng đầu tiên")).listing_ids==IDS[:4]


@pytest.mark.asyncio
async def test_bare_back_without_a_listing_context_is_left_to_the_existing_chat_routing():
    a=assistant()
    assert await a.try_answer("Quay lại",[],"no-listing") is None


@pytest.mark.asyncio
async def test_inactive_or_newly_unmatched_rooms_are_revalidated_inside_old_page_only():
    a,chat,_,_=await two_pages()
    a.tools.ids=[*IDS[1:4],*IDS[4:6]]
    a.tools.gone.add(IDS[2])
    result=await chat.ask("Quay lại")
    assert result.listing_ids==[IDS[1],IDS[3]]
    assert "không còn hiển thị" in result.answer and a.tools.payloads[-1]["scopeIds"]==IDS[:4]
    a.tools.ids=IDS[4:6]
    await chat.ask("Xem thêm")
    result=await chat.ask("Quay lại")
    assert result.status=="ANSWERED" and result.listing_ids==[]
    assert chat.state()["pageHistory"]["index"]==0
    assert (await chat.ask("Xem thêm")).listing_ids==IDS[4:6]


@pytest.mark.asyncio
async def test_navigation_is_invalidated_when_real_search_criteria_change():
    a,chat,_,_=await two_pages()
    a.planner.plan_result=search(city="HCM",ward="An Nhơn")
    await chat.ask("Chuyển sang tìm ở An Nhơn")
    assert chat.state()["pageHistory"]["pages"]==[IDS[:4]]
    before=len(a.tools.payloads)
    result=await chat.ask("Quay lại")
    assert "đang ở nhóm phòng đầu tiên" in result.answer and len(a.tools.payloads)==before


def legacy_messages():
    state=rs.merge(None,{"location":{"cityId":"12","wardId":"24588"},"sort":"newest"},"")
    state=rs.record_shown(state,IDS[:4],more=False)
    first=deepcopy(state);first.pop("pageHistory")
    state=rs.record_shown(state,IDS[4:6],more=True)
    last=deepcopy(state);last.pop("pageHistory")
    messages=[{"role":"assistant","status":"ANSWERED","content":"4 tin","listingContext":rs.listing_context(IDS[:4],first)},
        {"role":"assistant","status":"ANSWERED","content":"2 tin tiếp theo","listingContext":rs.listing_context(IDS[4:6],last)}]
    return last,messages


@pytest.mark.asyncio
async def test_existing_conversation_recovers_page_boundaries_from_the_whole_owned_history_after_exhaustion():
    last,messages=legacy_messages()
    last["revision"]=4;last["pending"]={"type":"EMPTY_RESULT","question":"Đổi điều kiện?","fields":[]}
    for i in range(14):
        messages += [{"role":"user","content":"Quay lại"}, {"role":"assistant","status":"NO_RESULTS","content":"Mình đã hiển thị hết các tin",
            "listingContext":rs.listing_context([],last)}]
    original=deepcopy(messages)
    memory=conversation_memory({"messages":messages,"renterSearchState":last})
    assert memory.repaired and len(memory.history)==8 and memory.state["revision"]==5
    assert memory.state["pageHistory"]=={"fingerprint":rs.fingerprint(memory.state),"pages":[IDS[:4],IDS[4:6]],"index":1}
    assert messages==original
    a=assistant()
    result=await a.try_answer("Quay lại",memory.history,"old-chat",state=memory.state)
    assert result.listing_ids==IDS[:4] and result.listing_context["searchState"]["revision"]>=5


def test_page_recovery_never_guesses_boundaries_from_prices_or_chunking_and_rejects_stale_query_history():
    state,messages=legacy_messages()
    recovered=recover(rs.sanitize(state),messages)
    assert recovered["pageHistory"]["pages"]==[IDS[:4],IDS[4:6]]
    changed=rs.merge(rs.sanitize(state),{"location":{"wardId":"24332"}},"")
    assert not recover(changed,messages)["pageHistory"]["pages"]
    dirty=deepcopy(recovered)
    dirty["pageHistory"]["pages"]=["not-a-page",["../../secret",*IDS[:4],IDS[9]]]
    dirty["pageHistory"]["index"]=999
    assert rs.sanitize(dirty)["pageHistory"]["pages"]==[IDS[:4]]
    assert rs.sanitize(dirty)["pageHistory"]["index"]==0


@pytest.mark.parametrize("text",["Quay lại phường An Nhơn", "Quay lại ngân sách 5 triệu", "Quay lại câu hỏi hợp đồng", "Tìm lại từ đầu", "Cho tôi xem thêm 10 phòng"])
def test_navigation_does_not_swallow_new_criteria_or_ambiguous_requests(text):
    assert intent(text) is None
