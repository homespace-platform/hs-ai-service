"""Listing reception hours, live availability and booking how-to must never be confused."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from homespace_ai.api.v1 import agent_ask
from homespace_ai.application import appointment_context as ctx
from homespace_ai.application.use_cases.appointment_ask import AppointmentAsk
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.core.config import get_settings
from homespace_ai.core.security import UserContext
from homespace_ai.tools.listings import ListingToolError


L1 = "10000000-0000-0000-0000-000000000001"
L2 = "10000000-0000-0000-0000-000000000002"
L3 = "10000000-0000-0000-0000-000000000003"
SCOPE = [L1, L2, L3]


def history():
    return [{"role": "assistant", "status": "ANSWERED", "content": "Các phòng vừa tìm",
             "listingContext": {"version": 1, "listingIds": SCOPE,
                                "mainRecommendation": {"listingId": L2, "fields": ["areaM2"]}}}]


def stored(result):
    return {"role": "assistant", "status": result.status, "content": result.answer,
            "listingContext": result.listing_context, "appointmentContext": result.appointment_context,
            "answerContext": result.answer_context}


@pytest.mark.parametrize("question", [
    "Tôi muốn xem lịch có thể đi xem của phòng được gợi ý.",
    "Tôi muốn xem lịch có thể xem của phòng được gợi ý.",
    "Lịch tiếp khách của Phòng 2 thế nào?",
    "Cho mình xem lịch xem phòng được đề xuất.",
    "toi muon xem lich co the di xem cua phong duoc goi y",
    "Tôi muốn xem lịch của phòng được gợi ý.",
    "Phòng được gợi ý nhận khách ngày nào?",
    "Các phòng trên có lịch tiếp khách thế nào?",
])
def test_listing_schedule_is_not_personal_appointments(question):
    assert ctx.classify(question, history()).kind == "LISTING_SCHEDULE"


@pytest.mark.parametrize("question", [
    "Tôi muốn đặt lịch xem phòng này, cần làm gì?",
    "Mình cần làm gì để đặt lịch xem Phòng 2?",
    "Tôi muốn đặt lịch xem phòng được gợi ý thì làm sao?",
    "Đặt lịch xem nhà như thế nào?",
    "toi muon dat lich xem phong nay can lam gi",
    "Ngày mai tôi muốn đặt lịch xem phòng này, cần chuẩn bị gì?",
    "Cách chọn khung giờ để đặt lịch xem phòng này như thế nào?",
])
def test_procedure_ignores_personal_markers_and_old_empty_appointments(question):
    previous = history() + [{"role": "assistant", "status": "NO_RESULTS", "content": "Chưa có lịch",
                            "appointmentContext": ctx.make_context([]), "listingContext": history()[0]["listingContext"]}]
    assert ctx.is_booking_guidance(question)
    assert ctx.classify(question, previous) is None


@pytest.mark.asyncio
async def test_schedule_reads_recommended_listing_not_user_bookings_and_keeps_reference():
    listings = SimpleNamespace(detail=AsyncMock(return_value={
        "id": L2, "viewingDays": ["SUNDAY", "TUESDAY"], "viewingSlots": ["EVENING", "MORNING"]}))
    private = SimpleNamespace(call=AsyncMock(side_effect=AssertionError("not a personal appointment query")))
    ask = AppointmentAsk(SimpleNamespace(generation_provider="disabled"), None, None, None,
                         listing_tools=listings, tools=private)
    result = await ask.try_answer("Tôi muốn xem lịch có thể đi xem của phòng được gợi ý.", history(), "r")
    listings.detail.assert_awaited_once_with(L2)
    private.call.assert_not_awaited()
    assert result.data_sources == ["CORE_LISTING"] and result.listing_ids == [L2]
    assert "| Lịch tiếp khách | Phòng 2 |" in result.answer
    assert "Thứ Ba, Chủ nhật" in result.answer
    assert "Buổi sáng; Buổi tối" in result.answer
    assert "Buổi chiều" not in result.answer and "hôm nay" not in result.answer
    assert "không có lịch bạn đăng ký" not in result.answer
    assert result.listing_context["listingIds"] == SCOPE
    assert result.listing_context["mainRecommendation"]["listingId"] == L2
    assert result.listing_context["viewingListingId"] == L2
    assert "Tôi muốn đặt lịch xem phòng này, cần làm gì?" in result.suggested_questions


@pytest.mark.asyncio
async def test_schedule_group_reads_each_listing_and_missing_hours_are_not_no_bookings():
    async def detail(item_id):
        return {"id": item_id, "viewingDays": ["MONDAY"] if item_id != L3 else [],
                "viewingSlots": ["AFTERNOON"] if item_id != L3 else []}
    listings = SimpleNamespace(detail=AsyncMock(side_effect=detail))
    ask = AppointmentAsk(SimpleNamespace(generation_provider="disabled"), None, None, None,
                         listing_tools=listings, tools=SimpleNamespace(call=AsyncMock()))
    result = await ask.try_answer("Các phòng trên có lịch tiếp khách thế nào?", history(), "r")
    assert [c.args[0] for c in listings.detail.await_args_list] == SCOPE
    assert "Phòng 1 | Phòng 2 | Phòng 3" in result.answer and "Tin chưa cung cấp" in result.answer
    assert "không có lịch" not in result.answer and result.appointment_cards == []


@pytest.mark.asyncio
async def test_no_recommendation_or_invalid_ordinal_requires_selection_not_invented_room():
    listings = SimpleNamespace(detail=AsyncMock())
    ask = AppointmentAsk(SimpleNamespace(generation_provider="disabled"), None, None, None,
                         listing_tools=listings, tools=SimpleNamespace(call=AsyncMock()))
    previous = history()
    previous[0]["listingContext"].pop("mainRecommendation")
    for question in ["Tôi muốn xem lịch có thể đi xem của phòng được gợi ý", "Lịch tiếp khách Phòng 9"]:
        result = await ask.try_answer(question, previous, "r")
        assert result.status == "NEEDS_CLARIFICATION"
    listings.detail.assert_not_awaited()


@pytest.mark.parametrize("reply", ["Ngày mai nhé", "Chiều mai", "Thứ Bảy tuần sau", "Phòng này ngày mai xem được không?"])
def test_short_day_reply_continues_selected_listing_not_empty_appointment_context(reply):
    references = {**history()[0]["listingContext"], "viewingListingId": L2}
    previous = [{"role": "assistant", "status": "ANSWERED", "listingContext": references,
                 "answerContext": {"intent": "VIEWING_SCHEDULE"}}]
    assert ctx.classify(reply, previous).kind == "AVAILABILITY"
    assert ctx.resolve_listing(reply, SCOPE, references)[0] == L2
    assert ctx.classify("Ngày mai trời mưa không?", previous) is None
    assert ctx.resolve_listing("Phòng 1 ngày mai xem được không?", SCOPE, references)[0] == L1


@pytest.mark.asyncio
async def test_real_orchestration_schedule_procedure_date_reply_calls_correct_read_only_paths(monkeypatch):
    calls, rag_questions = [], []

    class Gateway:
        async def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs.get("params")))
            assert method == "GET"
            if path == f"/api/v1/public/listings/ai/{L2}":
                data = {"id": L2, "status": "PUBLISHED", "active": True, "viewingDays": ["MONDAY", "TUESDAY"],
                        "viewingSlots": ["MORNING", "AFTERNOON"]}
            elif path == f"/api/v1/appointments/ai/listings/{L2}/availability":
                assert kwargs["params"] == {"datePreset": "TOMORROW"}
                data = {"listing": {"id": L2, "title": "Phòng được chọn"}, "date": "2026-11-02",
                        "dayAccepted": True, "slots": [{"startTime": "09:00", "endTime": "09:30", "status": "CAN_REQUEST"}]}
            else:
                raise AssertionError(f"Unexpected lookup: {path}")
            return httpx.Response(200, json={"result": data})

    async def rag(self, **kwargs):
        rag_questions.append(kwargs["question"])
        return AskResult("Các bước đặt lịch từ tài liệu đã duyệt.", "ANSWERED", [], "r")

    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: object())
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", rag)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway_client=Gateway())),
                              headers={"authorization": "Bearer test"})
    user = UserContext(**{"X-User-Id": "renter", "X-User-Role": "USER"})
    previous = history()

    async def turn(question):
        result = await agent_ask.generate_answer(question, previous, request, user, None, get_settings(), None)
        previous.extend([{"role": "user", "content": question}, stored(result)])
        return result

    schedule = await turn("Tôi muốn xem lịch có thể đi xem của phòng được gợi ý.")
    assert schedule.listing_ids == [L2] and len(calls) == 1 and not rag_questions
    guide = await turn("Tôi muốn đặt lịch xem phòng này, cần làm gì?")
    assert guide.data_sources == ["RAG"] and len(calls) == 1 and len(rag_questions) == 1
    assert guide.answer_context["domain"] == "KNOWLEDGE"
    assert guide.listing_context["viewingListingId"] == L2
    availability = await turn("Ngày mai nhé")
    assert availability.listing_ids == [L2] and "09:00–09:30" in availability.answer
    assert len(calls) == 2 and len(rag_questions) == 1


@pytest.mark.asyncio
async def test_guest_can_read_public_schedule_but_live_booking_availability_requires_login():
    listings = SimpleNamespace(detail=AsyncMock(return_value={"viewingDays": ["MONDAY"], "viewingSlots": ["MORNING"]}))
    private = SimpleNamespace(call=AsyncMock())
    ask = AppointmentAsk(SimpleNamespace(generation_provider="disabled"), None, None, None,
                         listing_tools=listings, tools=private, is_guest=True)
    result = await ask.try_answer("Lịch xem phòng được gợi ý", history(), "r")
    assert result.status == "ANSWERED"
    result = await ask.try_answer("Phòng được gợi ý ngày mai xem được không?", history(), "r")
    assert result.status == "LOGIN_REQUIRED"
    private.call.assert_not_awaited()


def test_mixed_availability_and_howto_keeps_both_data_sources():
    question = "Phòng 2 ngày mai xem được không, và đặt lịch thế nào?"
    assert not ctx.is_booking_guidance(question)
    route = ctx.classify(question, history())
    assert route.kind == "AVAILABILITY" and route.include_guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", [(ListingToolError("missing", status_code=404), "NOT_FOUND"),
                                          (TimeoutError(), "TOOL_UNAVAILABLE")])
async def test_listing_schedule_read_failure_is_never_reported_as_no_personal_bookings(error, status):
    listings = SimpleNamespace(detail=AsyncMock(side_effect=error))
    private = SimpleNamespace(call=AsyncMock())
    ask = AppointmentAsk(SimpleNamespace(generation_provider="disabled"), None, None, None,
                         listing_tools=listings, tools=private)
    result = await ask.try_answer("Lịch xem phòng được gợi ý", history(), "r")
    assert result.status == status and result.data_sources == ["CORE_LISTING"]
    assert "lịch tiếp khách" in result.answer
    assert "lịch hẹn của bạn" not in result.answer and "không có lịch" not in result.answer
    private.call.assert_not_awaited()

