"""Multi-result replies remain visual on follow-ups across the three read-only modules."""
import pytest

from homespace_ai.application.response_tables import markdown_table
from homespace_ai.clients import generative
from tests.unit import test_appointment_ask as appointments, test_contract_ask as contracts
from tests.unit.listing_fakes import IDS, Conversation
from tests.unit.test_listing_table import assistant_with, full_room, plain
from tests.unit.test_renter_guidance import assistant, complete_plan


@pytest.mark.asyncio
async def test_family_answer_after_search_keeps_four_columns_without_new_search_or_boundary():
    a = assistant()
    a.planner.plan_result = complete_plan()
    chat = Conversation(a)
    first = await chat.ask("Tìm phòng trọ phường Gò Vấp dưới 3,5 triệu cho 2 người")
    calls = (len(a.tools.payloads), len(a.tools.value_calls), a.planner.calls)
    a.tools.read.clear()
    result = await chat.ask("tôi muốn thuê cho gia đình")
    assert result.status == "ANSWERED" and result.listing_ids == first.listing_ids == IDS[:4]
    assert "| Đối chiếu cho gia đình | Phòng 1 | Phòng 2 | Phòng 3 | Phòng 4 |" in plain(result.answer)
    assert "| Điểm phù hợp để cân nhắc |" in result.answer and "| Ảnh |" not in result.answer
    assert a.tools.read == IDS[:4]
    assert (len(a.tools.payloads), len(a.tools.value_calls), a.planner.calls) == (calls[0]+1, calls[1], calls[2])
    assert a.tools.payloads[-1]["scopeIds"]==IDS[:4] and len(a.tools.nearby_calls)==1
    assert a.generative_client.evidence is None, "The persona reply needs a verified table, not a second prose narration"
    assert a.generative_client.boundary_calls[-1]["confirmedCriteria"]["renterProfile"]["persona"] == "FAMILY"


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Tư vấn giúp mình các lựa chọn trên", "Mình muốn biết các phòng này hợp với mình không"])
async def test_multi_listing_advice_always_has_table_even_without_section_or_comparison_keywords(question):
    a = assistant_with(None, {value: full_room(value) for value in IDS[:3]})
    history = [{"role": "assistant", "content": "3 phòng", "listingContext": {"version": 1, "listingIds": IDS[:3]}}]
    # Call the presentation boundary directly: the routing plan is not the behavior under test.
    evidence = {"listings": list(a.tools.details.values())}
    result = await a._present(question, history, "advice", "get_listing_details", evidence, IDS[:3], None, ids=None)
    assert "| Tiêu chí | Phòng 1 | Phòng 2 | Phòng 3 |" in plain(result.answer)
    assert result.listing_ids == IDS[:3] and evidence["tableRendered"]


def test_contract_results_and_disambiguation_use_the_same_table_and_keep_references():
    found = contracts.search(contracts.item(contracts.C1, "HD-1"), contracts.item(contracts.C2, "HD-2"), total=9)
    a, tools, model = contracts.make({"search_my_contracts": found})
    result = contracts.run(a.try_answer("Liệt kê các hợp đồng của tôi", [], "contract-table"))
    assert "| Hợp đồng | Số hợp đồng |" in result.answer
    assert "| Hợp đồng 1 | HD-1 |" in result.answer and "| Hợp đồng 2 | HD-2 |" in result.answer
    assert "Còn 7 hợp đồng" in result.answer and "15/01/2027" in result.answer
    assert result.contract_context["contractIds"] == [contracts.C1, contracts.C2]
    assert [c["contractId"] for c in result.contract_cards] == [contracts.C1, contracts.C2]
    assert len(tools.calls) == 1 and model.questions == []
    clarified = a._list_answer(found, "clarify", False, None, clarification=True)
    assert clarified.status == "NEEDS_CLARIFICATION" and "| Hợp đồng 2 | HD-2 |" in clarified.answer
    assert contracts.C1 not in result.answer and "ACTIVE" not in result.answer


def test_one_contract_and_empty_results_are_not_forced_into_a_table():
    a, _, _ = contracts.make({})
    one = a._list_answer(contracts.search(contracts.item(contracts.C1, "HD-1")), "one", False, None)
    empty = a._list_answer(contracts.search(), "empty", False, None)
    assert "| ---" not in one.answer and "| ---" not in empty.answer
    assert empty.status == "NO_RESULTS" and empty.contract_cards == []


def test_contract_summary_lists_its_verified_expiring_contracts_in_a_table_without_another_tool():
    summary = {"meta": contracts.META, "total": 8,
        "expiring": {"withinDays": 30, "count": 2,
            "contracts": [contracts.item(contracts.C1, "HD-1"), contracts.item(contracts.C2, "HD-2")]}}
    a, tools, _ = contracts.make({"summarize_my_contracts": summary})
    result = contracts.run(a.try_answer("Tôi có bao nhiêu hợp đồng?", [], "count-table"))
    assert "| Hợp đồng 1 | HD-1 |" in result.answer and "| Hợp đồng 2 | HD-2 |" in result.answer
    assert [name for name, _ in tools.calls] == ["summarize_my_contracts"]


def test_appointments_table_keeps_current_schedule_distinct_from_pending_reschedule_and_pagination():
    found = appointments.search(
        appointments.item(appointments.A1, role="OWNER", proposed=appointments.sched(start="16:00:00", end="16:30:00")),
        appointments.item(appointments.A2, role="RENTER", title="Căn hộ 2", status="PENDING"), total=8, page=2, size=5)
    a, _ = appointments.make({})
    result = a._list_result(found, "calendar-table", None, [appointments.L1])
    assert "| Lịch | Thời gian hiện tại | Tin đăng | Vai trò của bạn | Tình trạng |" in result.answer
    assert "| Lịch 6 |" in result.answer and "| Lịch 7 |" in result.answer
    assert "Đề xuất đổi giờ (chưa duyệt)" in result.answer and "16:00–16:30" in result.answer
    assert "14:00–14:30" in result.answer and "chưa được duyệt; lịch hiện tại vẫn là" in result.answer
    assert "Còn 1 lịch nữa" in result.answer
    assert result.appointment_context["appointmentIds"] == [appointments.A1, appointments.A2]
    assert result.appointment_context["page"]["page"] == 2
    assert appointments.no_raw_values(result.answer)


def test_availability_multiple_slots_is_tabular_and_does_not_claim_booking():
    a, _ = appointments.make({})
    data = appointments.availability([
        appointments.slot("14:00:00", "14:30:00"),
        appointments.slot("14:30:00", "15:00:00", "YOUR_REQUEST_PENDING"),
        appointments.slot("15:00:00", "15:30:00", "NOT_AVAILABLE"),
    ])
    text = a._availability_text(data, None, None)
    assert "| Khung giờ | Tình trạng |" in text
    assert "| 14:00–14:30 | Có thể gửi yêu cầu |" in text
    assert "| 14:30–15:00 | Yêu cầu của bạn đang chờ xác nhận |" in text
    assert "| 15:00–15:30 | Không đặt được |" in text
    assert "chưa phải lịch đã được giữ chỗ" in text and appointments.no_raw_values(text)
    data["slots"] = []
    assert "| ---" not in a._availability_text(data, None, None)


def test_appointment_count_and_planned_detail_batches_also_use_tables_without_extra_calls():
    items = [appointments.item(appointments.A1), appointments.item(appointments.A2)]
    a, tools = appointments.make({"summarize_my_appointments": appointments.summary(total=2),
        "search_my_appointments": appointments.search(*items)})
    result = appointments.ask(a, "Tôi có mấy buổi xem nhà tháng này?")
    assert "| Lịch 1 |" in result.answer and "| Lịch 2 |" in result.answer
    assert len(tools.calls) == 2
    details = [appointments.detail(appointments.A1), appointments.detail(appointments.A2)]
    batch = a._compose_planned([("get_appointment_detail", {}, data) for data in details], "batch", None, [])
    assert "| Lịch 1 |" in batch.answer and "| Lịch 2 |" in batch.answer
    assert batch.appointment_context["appointmentIds"] == [appointments.A1, appointments.A2]
    assert "Thời gian hiện tại" in batch.answer and len(tools.calls) == 2


@pytest.mark.parametrize("prompt", [generative.SYSTEM_PROMPT, generative.ADMIN_RAG_PROMPT,
    generative.LISTING_SYSTEM_PROMPT, generative.CONTRACT_SYSTEM_PROMPT, generative.RENTAL_REVIEW_SYSTEM_PROMPT])
def test_all_customer_generation_modes_share_the_multi_result_table_policy(prompt):
    assert generative.MULTI_RESULT_FORMAT_PROMPT in prompt
    assert "câu hỏi tiếp theo" in prompt and "tableRendered" in prompt


def test_tables_escape_untrusted_markup_and_preserve_zero_false_and_empty_values():
    text = markdown_table(["Tên", "Số lượng"], [["A | B\n[link](https://bad.invalid)<script>*x*", 0], [None, False]])
    assert len(text.splitlines()) == 4 and r"A \| B" in text
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert r"\[link\](https://bad.invalid)" in text
    assert "| 0 |" in text and "| Chưa có thông tin | False |" in text
    assert markdown_table(["Tên"], []) == ""
    with pytest.raises(ValueError):
        markdown_table(["Tên"], [["x", "y"]])
