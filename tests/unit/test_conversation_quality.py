"""Reported fee failure, proof competition, and next questions across read-only domains."""
from copy import deepcopy
from unittest.mock import AsyncMock
import json
import httpx
import pytest

from homespace_ai.application import listing_value_advice as value
from homespace_ai.application.answer_context import scoped_facts
from homespace_ai.application.conversation_guidance import finish
from homespace_ai.application.listing_recommendation_explanation import explain
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.clients.generative import OpenAIGenerativeClient
from homespace_ai.tools.listings import ListingTools, ListingToolError
from tests.unit.listing_fakes import IDS, make_assistant, Conversation, room, search, c
from tests.unit.test_listing_value_advice import CAT
from tests.unit.test_openai_generation import completed, mock_http, settings


def fee_rooms():
    items = []
    for i,(rent,area) in enumerate(zip([3800000,3900000,3300000,3800000],[30,32,30,37])):
        items.append(room(IDS[i], areaM2=area, pricing={"amount":rent,"unit":"ROOM_MONTH","currency":"VND"},
            roomDetail={"maxOccupants":3,"kitchenType":"PRIVATE" if i==0 else "SHARED" if i==1 else "NONE",
                        "restroomType":"SHARED" if i<2 else "PRIVATE"},
            charges=[{"chargeType":"INTERNET","includedInRent":i==0,"billingMethod":"PER_MONTH","amount":[0,90000,120000,120000][i]},
                     {"chargeType":"MOTORBIKE_PARKING","includedInRent":i!=0,"billingMethod":"PER_VEHICLE_MONTH","amount":80000},
                     {"chargeType":"SERVICE_OR_GARBAGE","includedInRent":False,"billingMethod":"PER_PERSON_MONTH","amount":[40000,30000,45000,30000][i]}]))
    return items


def choices():
    return {"selectedId":None,"comparedListingIds":IDS[:4],"advantages":[],"tradeoffs":[],
        "mainRecommendation":{"listingId":IDS[2],"fields":["charges.MOTORBIKE_PARKING.includedInRent"]},
        "priorityRecommendations":[{"focus":"COST","listingId":IDS[2],"fields":["pricing.amount"]},
            {"focus":"SPACE","listingId":IDS[3],"fields":["areaM2"]},
            {"focus":"AMENITIES","listingId":IDS[0],"fields":["roomDetail.kitchenType"]}]}


async def prepared():
    a=make_assistant(search(c("category","eq","ROOM"),c("rent.monthly_amount","lt",4000000),ward="Gò Vấp",city="HCM",context={"occupants":3}),IDS[:4])
    a.tools.details={x["id"]:x for x in fee_rooms()}
    a.generative_client.boundary_decision=choices()
    chat=Conversation(a)
    await chat.ask("Tôi cần tìm phòng trọ phường Gò Vấp cho 3 người giá dưới 4 triệu")
    return a,chat


@pytest.mark.asyncio
async def test_fee_answer_table_first_short_conclusion_priorities_without_shared_parking_claim():
    a,chat=await prepared()
    before=(a.tools.search_calls,len(a.tools.value_calls))
    a.generative_client.kwargs=None
    result=await chat.ask("Trong các phòng này, các khoản phí hằng tháng khác nhau thế nào?")
    assert result.status=="ANSWERED" and result.listing_ids==IDS[:4]
    assert a.generative_client.kwargs is None  # no free-text introduction competing with verified advice
    assert "| Internet |" in result.answer and "| Ảnh |" not in result.answer
    assert "| Nếu bạn ưu tiên |" in result.answer
    assert "3.300.000" in result.answer and "37 m²" in result.answer and "bếp riêng" in result.answer
    assert result.answer.index("| Tiêu chí |") < result.answer.index("| Nếu bạn ưu tiên |")
    assert "vì tiền thuê đã bao gồm **phí gửi xe máy**" not in result.answer
    assert "bạn ưu tiên tiết kiệm" not in result.answer and "tổng chi phí thấp nhất" not in result.answer
    assert (a.tools.search_calls,len(a.tools.value_calls))==before and not a.tools.nearby_calls
    assert result.listing_context["mainRecommendation"]["fields"]==["pricing.amount"]
    assert result.listing_context["mainRecommendation"]["focus"]=="COST"
    assert not any("khác nhau" in q and "phí hằng tháng" in q for q in result.suggested_questions)
    assert len(result.suggested_questions)<=4 and result.answer_context["intent"]=="COST"
    assert not any("xung quanh" in q or "chi phí cố định" in q for q in result.suggested_questions)
    assert "Tổng cố định" not in result.answer and "tính phần cố định" not in result.answer


@pytest.mark.parametrize("question", [
    "Bạn so giúp các khoản phí phải trả thêm mỗi tháng của mấy phòng này.",
    "Phòng nào đỡ tốn tiền hơn nếu tính cả các khoản phí?",
    "Trong các phòng này, các khoản phí hằng tháng khác nhau thế nào?"])
@pytest.mark.asyncio
async def test_natural_fee_paraphrases_keep_the_known_scope_without_new_search(question):
    a, chat = await prepared()
    before = a.tools.search_calls, a.planner.calls, len(a.tools.value_calls)
    result = await chat.ask(question)
    assert result.answer_context["intent"] == "COST"
    assert result.listing_ids == IDS[:4]
    assert (a.tools.search_calls, a.planner.calls, len(a.tools.value_calls)) == before


@pytest.mark.parametrize("index",[1,2,3])
@pytest.mark.asyncio
async def test_shared_subset_benefit_cannot_alone_pick_a_specific_peer(index):
    model=AsyncMock()
    model.generate_value_advice.return_value=choices()|{"mainRecommendation":{"listingId":IDS[index],"fields":["charges.MOTORBIKE_PARKING.includedInRent"]}}
    result=await value.evaluate("So phí",{},fee_rooms(),[],{},model,CAT,recommend_main=True,recommendation_focus="COST")
    assert result["main"] is None


@pytest.mark.asyncio
async def test_visible_conditional_choice_is_also_the_stored_main_reference_for_that_objective():
    model = AsyncMock()
    model.generate_value_advice.return_value = choices() | {
        "mainRecommendation":{"listingId":IDS[0],"fields":["charges.INTERNET.includedInRent"]}}
    result = await value.evaluate("So phí", {}, fee_rooms(), [], {}, model, CAT,
                                  recommend_main=True, recommendation_focus="COST", compare_priorities=True)
    assert result["main"]["listingId"] == IDS[2]
    assert next(p for p in result["priorities"] if p["focus"] == "COST")["listingId"] == IDS[2]


@pytest.mark.parametrize("focus,field,index",[("COST","pricing.amount",2),("SPACE","areaM2",3),("AMENITIES","roomDetail.kitchenType",0)])
@pytest.mark.asyncio
async def test_a_real_distinction_in_the_current_objective_is_eligible(focus,field,index):
    model=AsyncMock()
    model.generate_value_advice.return_value=choices()|{"mainRecommendation":{"listingId":IDS[index],"fields":[field]}}
    result=await value.evaluate("So sánh",{},fee_rooms(),[],{},model,CAT,recommend_main=True,recommendation_focus=focus)
    assert result["main"]["listingId"]==IDS[index]


@pytest.mark.asyncio
async def test_cost_judge_cannot_replace_cost_reason_with_kitchen_or_wc():
    model=AsyncMock()
    model.generate_value_advice.return_value=choices()|{"mainRecommendation":{"listingId":IDS[0],"fields":["roomDetail.kitchenType"]}}
    result=await value.evaluate("So phí",{},fee_rooms(),[],{},model,CAT,recommend_main=True,recommendation_focus="COST")
    assert result["main"] is None
    assert all(p.startswith(("pricing.","charges.","cost.")) for p in model.generate_value_advice.await_args.kwargs["evidence"]["factMatrix"])


@pytest.mark.asyncio
async def test_full_chain_cost_to_space_to_why_keeps_scope_and_objective():
    a,chat=await prepared()
    fee=await chat.ask("Trong các phòng này, các khoản phí hằng tháng khác nhau thế nào?")
    a.generative_client.boundary_decision["mainRecommendation"]={"listingId":IDS[3],"fields":["areaM2"]}
    wider=await chat.ask("Nếu ưu tiên không gian rộng hơn thì bạn chọn phòng nào?")
    assert wider.listing_context["mainRecommendation"]["listingId"]==IDS[3]
    assert wider.listing_context["mainRecommendation"]["focus"]=="SPACE"
    a.generative_client.generate_main_explanation=AsyncMock(return_value={"reasons":["areaM2"],"tradeoffs":[]})
    why=await chat.ask("Vì sao?")
    assert "| Tiêu chí đối chiếu |" in why.answer and "37 m²" in why.answer
    assert "phí gửi xe" not in why.answer and "3.300.000" not in why.answer
    assert why.listing_context["mainRecommendation"]["listingId"]==IDS[3]
    assert why.listing_ids==IDS[:4] and not a.tools.nearby_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Vì sao bạn chọn phương án tiết kiệm?", "Tại sao bạn ưu tiên phương án tiết kiệm?"])
async def test_saving_is_not_mistaken_for_a_new_find_command_when_explaining(question):
    a, chat = await prepared()
    await chat.ask("So sánh chi phí các phòng trên")
    before = a.tools.search_calls, len(a.tools.value_calls), a.planner.calls
    a.generative_client.generate_main_explanation = AsyncMock(return_value={"reasons":["pricing.amount"],"tradeoffs":[]})
    result = await chat.ask(question)
    assert "| Tiêu chí đối chiếu |" in result.answer
    assert result.listing_context["mainRecommendation"]["listingId"] == IDS[2]
    assert (a.tools.search_calls, len(a.tools.value_calls), a.planner.calls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("question,context", [
    ("Bọn mình có 3 người, 2 xe máy.", {"occupants":3,"motorbikes":2}),
    ("Mỗi tháng dùng khoảng 100 kWh, 8 khối nước.", {"electricityKwh":100,"waterM3":8})])
async def test_new_counts_and_usage_go_through_validated_criteria_instead_of_a_stale_detail_shortcut(question, context):
    a, chat = await prepared()
    before = len(a.tools.value_calls)
    a.planner.plan_result = search(context=context)
    await chat.ask(question)
    saved = chat.state()["context"]
    assert all(saved[key] == value for key,value in context.items())
    assert saved["occupants"] == 3
    assert a.planner.calls == 2 and len(a.tools.value_calls) == before


def test_fee_answers_never_offer_fixed_cost_calculation():
    result = finish(answer(), "Với số người mình đã nói, tổng chi phí cố định mỗi tháng ra sao?", [], "LISTING")
    assert "COST_TOTAL" not in result.answer_context["topics"]
    assert not any("phần chi phí cố định" in q for q in result.suggested_questions)


@pytest.mark.asyncio
async def test_cost_tool_is_one_bounded_batch_with_no_defaults_for_missing_inputs():
    gateway = AsyncMock()
    gateway.request.return_value = httpx.Response(200, json={"result":{"items":[{
        "listingId":IDS[0], "fixedMonthlyTotal":None, "estimatedMonthlyTotal":None,
        "knownFixedMonthly":3920000, "missingInputs":["motorbikes"], "motorbikesAllowed":None}]}})
    tool = ListingTools(gateway, object(), 20)
    result = await tool.costs(IDS[:4], {"occupants":3})
    assert result["items"][0]["fixedMonthlyTotal"] is None
    gateway.request.assert_awaited_once()
    assert gateway.request.await_args.kwargs["json"] == {"listingIds":IDS[:4], "context":{"occupants":3}}


@pytest.mark.parametrize("bad", [
    {"listingId":IDS[5]}, {"fixedMonthlyTotal":-1}, {"estimatedMonthlyTotal":"NaN"},
    {"fixedMonthlyTotal":True}, {"motorbikesAllowed":"true"}, {"missingInputs":["invented"]}])
@pytest.mark.asyncio
async def test_invalid_cost_projection_cannot_supply_ranking_facts(bad):
    gateway = AsyncMock()
    gateway.request.return_value = httpx.Response(200, json={"result":{"items":[{
        "listingId":IDS[0], "missingInputs":[], **bad}]}})
    with pytest.raises(ListingToolError):
        await ListingTools(gateway, object(), 20).costs(IDS[:4], {})


@pytest.mark.parametrize("domain,question,blocked", [
    ("CONTRACT","Các khoản phí trong hợp đồng này là gì?","CHARGES"),
    ("CONTRACT","Ngày cuối cùng của hợp đồng này là ngày nào?","TERM"),
    ("APPOINTMENT","Lịch hẹn này chủ nhà xác nhận chưa?","STATUS")])
def test_domain_suggestions_remember_recently_answered_topics_after_another_turn(domain,question,blocked):
    kwargs = ({"contract_context":{"contractIds":IDS[:1]}, "contract_cards":[{"status":"ACTIVE","contractId":IDS[0]}]} if domain=="CONTRACT" else
              {"appointment_context":{"appointmentIds":IDS[:1]},"appointment_cards":[{"status":"PENDING","appointmentId":IDS[0]}]})
    first = finish(answer(domain, **kwargs), question, [], domain)
    assert blocked in first.answer_context["topics"]
    second = finish(answer(domain, **kwargs), "Cho tôi xem chi tiết", [{"role":"assistant","answerContext":first.answer_context}], domain)
    forbidden = {"CHARGES":"Các khoản phí đang áp dụng", "TERM":"Ngày cuối cùng", "STATUS":"đang chờ bên nào phản hồi"}[blocked]
    assert not any(forbidden in q for q in second.suggested_questions)


@pytest.mark.asyncio
async def test_fee_comparison_does_not_fetch_or_render_a_cost_projection():
    a,chat=await prepared()
    a.tools.costs=AsyncMock(return_value={"items":[{"listingId":i,"monthlyRent":3300000,"knownFixedMonthly":3555000,
        "fixedMonthlyTotal":None,"estimatedMonthlyTotal":None,"missingInputs":["motorbikes","electricityKwh","waterM3"]} for i in IDS[:4]]})
    for item in a.tools.details.values():
        item["_costEvidence"] = {"monthlyRent":3300000,"knownFixedMonthly":3555000,
                                 "fixedMonthlyTotal":3555000,"missingInputs":["motorbikes"]}
    result=await chat.ask("So sánh chi phí các phòng trên")
    a.tools.costs.assert_not_awaited()
    assert "Phần cố định đã tính được" not in result.answer and "Tổng cố định" not in result.answer
    assert "số xe máy" not in result.answer
    assert "cost.fixedMonthlyTotal" not in a.generative_client.boundary_calls[-1]["factMatrix"]
    assert "3.555.000" not in result.answer


@pytest.mark.asyncio
async def test_cost_failure_keeps_verified_tariff_table_and_no_invented_totals():
    a,chat=await prepared()
    a.tools.costs=AsyncMock(side_effect=ListingToolError("timeout"))
    result=await chat.ask("So sánh chi phí các phòng trên")
    assert result.status=="ANSWERED" and "| Internet |" in result.answer
    a.tools.costs.assert_not_awaited()
    assert "Tổng cố định" not in result.answer and "tính phần cố định" not in result.answer


@pytest.mark.asyncio
async def test_explicit_rate_only_question_has_only_requested_fee_rows_and_no_forced_advice():
    a, chat = await prepared()
    for item in a.tools.details.values():
        item["charges"] += [{"chargeType":"ELECTRICITY","amount":3500,"unit":"kWh"},
                            {"chargeType":"WATER","amount":12000,"unit":"m³"}]
    before = len(a.generative_client.boundary_calls)
    a.tools.costs = AsyncMock()
    result = await chat.ask("Tôi chỉ muốn xem đơn giá điện nước của các phòng trên thôi.")
    assert "| Điện |" in result.answer and "| Nước |" in result.answer
    assert "| Internet |" not in result.answer and "| Gửi xe máy |" not in result.answer
    assert "Nếu bạn ưu tiên" not in result.answer and "Mình gợi ý" not in result.answer
    assert len(a.generative_client.boundary_calls) == before
    a.tools.costs.assert_not_awaited()


@pytest.mark.asyncio
async def test_disallowed_vehicle_capacity_cannot_win_cost_recommendation():
    items=fee_rooms()
    items[2]["_costEvidence"]={"fixedMonthlyTotal":3555000,"motorbikesAllowed":False}
    model=AsyncMock();model.generate_value_advice.return_value=choices()|{"mainRecommendation":{"listingId":IDS[2],"fields":["pricing.amount"]}}
    result=await value.evaluate("Hai xe máy",{},items,[],{},model,CAT,recommend_main=True,recommendation_focus="COST")
    assert result["main"] is None


@pytest.mark.asyncio
async def test_legacy_cost_projection_does_not_supply_new_comparison_facts():
    items = fee_rooms()
    items[2]["pricing"].update(amount=1100000, unit="PERSON_MONTH")
    items[2]["_costEvidence"] = {"monthlyRent":3300000}
    model = AsyncMock()
    model.generate_value_advice.return_value = choices() | {"mainRecommendation":{"listingId":IDS[2],"fields":["pricing.amount"]}}
    result = await value.evaluate("So tiền thuê cho 3 người", {}, items, [], {}, model, CAT,
                                  recommend_main=True, recommendation_focus="COST")
    assert result["main"] is None
    assert not any(path.startswith("cost.") for path in model.generate_value_advice.await_args.kwargs["evidence"]["factMatrix"])


def answer(domain="LISTING",**kw):
    return AskResult(answer="Dữ liệu đã đọc",status="ANSWERED",citations=[],request_id="test",
        listing_context={"listingIds":IDS[:4],"searchState":{"context":{"occupants":3}},"hasMore":True} if domain=="LISTING" else None,**kw)


@pytest.mark.parametrize("q,blocked",[("So sánh chi phí các phòng trên","COST"),("Các phòng trên có tiện ích nào xung quanh?","NEARBY"),("Vì sao bạn ưu tiên phòng được chọn?","EXPLAIN_CHOICE")])
def test_suggestions_exclude_current_answer_topic(q,blocked):
    result=finish(answer(),q,[],"LISTING")
    from homespace_ai.application.answer_context import listing_focus
    if blocked == "COST":
        # A missing-context total is a next step, not a repeat of the tariff comparison.
        assert not any("phí hằng tháng khác nhau" in chip for chip in result.suggested_questions)
    else:
        assert not any(listing_focus(chip)==blocked for chip in result.suggested_questions)


@pytest.mark.parametrize("status",["DRAFT","TENANT_SIGNATURE_PENDING","ACTIVE","CLOSED"])
def test_contract_suggestions_follow_verified_signing_state(status):
    result=answer("CONTRACT",contract_context={"contractIds":IDS[:1]},contract_cards=[{"status":status,"contractId":IDS[0]}])
    finish(result,"Hợp đồng này hiện thế nào?",[],"CONTRACT")
    assert result.suggested_questions
    assert any("ký" in x for x in result.suggested_questions)==(status in {"DRAFT","TENANT_SIGNATURE_PENDING"})
    assert not any("phòng trên" in x for x in result.suggested_questions)


@pytest.mark.parametrize("status,reschedule",[("PENDING",False),("CONFIRMED",True),("CANCELLED",False),("COMPLETED",False)])
def test_appointment_suggestions_do_not_claim_pending_schedule_or_actions_completed(status,reschedule):
    result=answer("APPOINTMENT",appointment_context={"appointmentIds":IDS[:1]},appointment_cards=[{"appointmentId":IDS[0],"status":status,"rescheduleRequested":reschedule}])
    finish(result,"Cho xem chi tiết lịch hẹn này",[],"APPOINTMENT")
    assert any("xin đổi" in x for x in result.suggested_questions)==reschedule
    assert any("thao tác" in x for x in result.suggested_questions)==(status in {"PENDING","CONFIRMED"})
    assert not any("đã đặt" in x for x in result.suggested_questions)


def test_no_next_page_when_exhausted_and_prior_topics_survive_reload():
    first=answer();first.listing_context["hasMore"]=False
    finish(first,"So sánh chi phí các phòng trên",[],"LISTING")
    history=[{"role":"assistant","answerContext":first.answer_context,"listingContext":first.listing_context}]
    second=answer();second.listing_context.pop("hasMore")
    finish(second,"Các phòng trên có gì xung quanh?",history,"LISTING")
    assert not any("xem thêm" in q or "phí hằng tháng" in q for q in second.suggested_questions)
    assert "3555000" not in str(second.answer_context) and second.answer_context["scope"]==IDS[:4]


@pytest.mark.asyncio
async def test_native_priority_schema_allows_abstention_and_preserves_focus(monkeypatch):
    raw=choices()|{"candidateAssessments":[],"mainRecommendation":{"listingId":None,"fields":[]}}
    calls=mock_http(monkeypatch,[httpx.Response(200,json=completed(json.dumps(raw)))])
    await value.evaluate("So phí",{},fee_rooms(),[],{},OpenAIGenerativeClient(settings()),CAT,
                         recommend_main=True,recommendation_focus="COST",compare_priorities=True)
    schema=json.loads(calls[0].content)["text"]["format"]["schema"]
    assert "priorityRecommendations" in schema["required"]
    assert None in schema["properties"]["mainRecommendation"]["properties"]["listingId"]["enum"]
