from datetime import date

import pytest

from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.listing_catalog import SearchCatalog
from homespace_ai.application.listing_street import administrative_text, street_mentions
from homespace_ai.application.renter_criteria import normalize
from homespace_ai.application.renter_guidance import explicit_location
from homespace_ai.clients.listing_tool_planner import ToolPlan
from tests.unit.listing_fakes import CATALOG, IDS, Conversation, c, make_assistant, search


CAT = SearchCatalog(CATALOG)


@pytest.mark.parametrize("question,street", [
    ("Tìm phòng trọ đường Phan Văn Trị cho 3 người giá dưới 4 triệu", "Phan Văn Trị"),
    ("Tìm phòng trọ đường Phan Văn Trị cho 2 người giá dưới 4 triệu", "Phan Văn Trị"),
    ("tim phong tro duong Phan Van Tri cho 3 nguoi gia duoi 4 trieu", "Phan Van Tri"),
    ("Tìm phòng trọ ở đường Nguyễn Oanh, phường Gò Vấp, HCM dưới 4 triệu", "Nguyễn Oanh"),
    ("Tìm chung cư trên Đ. Lê Lợi, HCM giá tối đa 10 triệu", "Lê Lợi"),
    ("Tìm phòng đường Số 4, HCM dưới 5 triệu", "Số 4"),
    ("Tìm phòng đường 3 Tháng 2 tại TP.HCM cho 2 người dưới 4 triệu", "3 Tháng 2"),
])
def test_street_extraction_does_not_swallow_other_conditions(question, street):
    assert street_mentions(question)[0][0] == street


@pytest.mark.parametrize("question", ["Chỉ đường đến phòng này", "Đường đi đến trường có xa không?",
                                      "Phòng có đường truyền Internet tốt không?",
                                      "Mình làm điều dưỡng ca đêm, bạn tư vấn phòng nào hợp với mình?",
                                      "Tôi bị tiểu đường nên cần ở gần bệnh viện"])
def test_non_address_road_wording_is_not_a_street_filter(question):
    assert street_mentions(question) == []


def test_street_name_is_never_a_ward_even_when_model_puts_it_in_location():
    question = "Tìm phòng trọ đường Phan Văn Trị cho 3 người giá dưới 4 triệu"
    update = normalize(question, search(ward="Phan Văn Trị").arguments, CAT, None, date(2026, 11, 1))
    assert update["location"] is None and update["placeQuery"] is None
    assert {(x["field"], x["op"], str(x["value"])) for x in update["constraints"]} >= {
        ("street", "contains", "Phan Văn Trị"), ("category", "eq", "ROOM"),
        ("occupancy.max_occupants", "gte", "3"), ("rent.monthly_amount", "lt", "4000000")}


def test_street_named_after_a_ward_does_not_add_that_ward():
    question = "Tìm phòng trọ đường Hạnh Thông, HCM cho 2 người dưới 4 triệu"
    update = normalize(question, search(ward="Hạnh Thông").arguments, CAT, None, date(2026, 11, 1))
    assert update["location"] == {"ward": None, "city": "Thành phố Hồ Chí Minh"}
    assert "Hạnh Thông" not in administrative_text(question)
    assert explicit_location(question, []) == {"cityId": "12", "city": "Thành phố Hồ Chí Minh"}


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", [search(ward="Phan Văn Trị"),
                                  ToolPlan("ask_clarification", {"missing": ["LOCATION"], "question": "Bạn tìm ở đâu?", "options": []})])
async def test_original_question_keeps_street_and_asks_only_city_then_searches(plan):
    assistant = make_assistant(plan, IDS[:4])
    assistant._require_search_basics = True
    chat = Conversation(assistant)
    result = await chat.ask("Tìm phòng trọ đường Phan Văn Trị cho 3 người giá dưới 4 triệu")
    assert assistant.tools.search_calls == 0
    assert result.status == "NEEDS_CLARIFICATION" and "đường Phan Văn Trị ở tỉnh/thành phố nào" in result.answer
    assert "ghép được phường/xã" not in result.answer
    assert any(x["field"] == "street" for x in chat.state()["constraints"])
    assistant.planner.plan_result = search(city="HCM")
    result = await chat.ask("Ở TP.HCM nhé")
    assert result.status == "ANSWERED" and assistant.tools.search_calls == 1
    payload = assistant.tools.payloads[-1]
    leaves = payload["where"]["all"]
    assert any(x == {"field": "street", "op": "contains", "value": "Phan Văn Trị"} for x in leaves)
    assert any(x["field"] == "city_id" and x["value"] == "12" for x in leaves)
    assert not any(x["field"] == "ward_id" for x in leaves)
    assert payload["context"]["occupants"] == 3
    assistant.planner.plan_result = search()
    await chat.ask("Cho 2 người thôi, tăng lên dưới 5 triệu")
    payload = assistant.tools.payloads[-1]
    assert payload["context"]["occupants"] == 2
    assert any(x["field"] == "street" and x["value"] == "Phan Văn Trị" for x in payload["where"]["all"])
    assert any(x["field"] == "rent.monthly_amount" and x["op"] == "lt" and str(x["value"]) == "5000000"
               for x in payload["where"]["all"])


@pytest.mark.asyncio
async def test_explicit_city_street_search_runs_immediately_without_nearby_resolution():
    assistant = make_assistant(search(ward="Phan Văn Trị", place="Phan Văn Trị"), IDS[:4])
    assistant._require_search_basics = True
    result = await Conversation(assistant).ask("Tìm phòng trọ đường Phan Văn Trị, TP.HCM cho 2 người giá dưới 4 triệu")
    assert result.status == "ANSWERED" and assistant.tools.search_calls == 1
    assert assistant.tools.place_queries == []
    assert any(x["field"] == "street" for x in assistant.tools.payloads[0]["where"]["all"])


def test_switch_street_replaces_old_street_instead_of_anding_two_streets():
    first = normalize("Tìm phòng đường Phan Văn Trị, HCM", search().arguments, CAT, None, date(2026, 11, 1))
    state = rs.merge(None, first, "")
    second = normalize("Chuyển sang đường Nguyễn Oanh", search().arguments, CAT, state, date(2026, 11, 1))
    state = rs.merge(state, second, "")
    assert [x["value"] for x in state["constraints"] if x["field"] == "street"] == ["Nguyễn Oanh"]


def test_on_street_does_not_require_the_title_to_contain_the_street_name():
    update = normalize("Tìm phòng trọ đường Phan Văn Trị, HCM", search(
        c("title", "contains", "Phan Văn Trị"), keyword="Phan Văn Trị").arguments,
        CAT, None, date(2026, 11, 1))
    assert update["keyword"] is None
    assert not any(c["field"] == "title" for c in update["constraints"])
    assert any(c["field"] == "street" for c in update["constraints"])


def test_near_a_street_is_not_reinterpreted_as_on_that_street():
    update = normalize("Tìm phòng gần đường Phan Văn Trị, HCM", search(place="Phan Văn Trị").arguments,
                       CAT, None, date(2026, 11, 1))
    assert not any(c["field"] == "street" for c in update["constraints"])
    assert update["placeQuery"] == "Phan Văn Trị"

