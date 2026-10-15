from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from homespace_ai.api.v1 import agent_ask
from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.api.v1.schemas import ConversationMessage
from homespace_ai.application.appointment_context import make_context
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.core.database import get_db
from homespace_ai.core.security import UserContext, get_current_user
from homespace_ai.main import app

A1 = "00000000-0000-0000-0000-000000000001"
CARD = {"appointmentId": A1, "requesterRole": "RENTER", "roleLabel": "Người đi xem",
        "listingId": "10000000-0000-0000-0000-000000000001", "listingTitle": "Căn hộ Mẫu", "listingAddress": "1 Đường Mẫu",
        "date": "2026-10-14", "startTime": "14:00", "endTime": "14:30", "status": "CONFIRMED",
        "statusLabel": "Đã xác nhận", "temporalPhase": "UPCOMING", "phaseLabel": "Sắp diễn ra",
        "proposedDate": None, "readAt": "2026-10-14T03:30:00Z", "source": "CORE"}


async def fake_db():
    yield None


async def _none():
    return None


def setup(monkeypatch, user_id="renter"):
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_embedder] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: UserContext(**{"X-User-Id": user_id, "X-User-Role": "USER"})
    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: object())


@pytest.mark.asyncio
async def test_personal_appointment_question_skips_rag_and_contract_and_persists_references_and_cards(monkeypatch):
    setup(monkeypatch)
    context = make_context([A1])
    appended, seen = [], {}

    class Repository:
        async def get(self, owner_id, conversation_id):
            assert owner_id == "renter"  # only the caller's own conversation is ever read
            return {"messages": [{"role": "assistant", "content": "x", "appointmentContext": context}]}

        async def append_message(self, owner_id, conversation_id, **message):
            appended.append((owner_id, message))
            return True

    async def fake_appointment(self, question, history, request_id):
        seen["history"] = history
        return AskResult(answer="Theo dữ liệu hệ thống ...", status="ANSWERED", citations=[], request_id=request_id,
                         appointment_context=context, appointment_cards=[CARD], data_sources=["CORE_APPOINTMENT"])

    async def fail(self, *args, **kwargs):
        raise AssertionError("must not reach RAG, Contract or Listing")

    monkeypatch.setattr(agent_ask, "get_conversation_repository", lambda: Repository())
    monkeypatch.setattr(agent_ask.AppointmentAsk, "try_answer", fake_appointment)
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", fail)
    monkeypatch.setattr(agent_ask.ContractAsk, "try_answer", fail)
    monkeypatch.setattr(agent_ask.ListingAsk, "try_answer", fail)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/agent/ask", json={"question": "Hôm nay tôi có lịch xem nhà nào?",
                                                              "conversationId": "c-1"})
        body = response.json()["result"]
        assert response.status_code == 200 and body["status"] == "ANSWERED"
        assert body["appointmentCards"][0]["appointmentId"] == A1 and body["appointmentCards"][0]["status"] == "CONFIRMED"
        assert body["dataSources"] == ["CORE_APPOINTMENT"] and body["contractCards"] == []
        assert "appointmentContext" not in body  # internal state, like listingContext
        assert seen["history"][0]["appointmentContext"] == context
        owner, saved = appended[-1]
        assert owner == "renter" and saved["appointment_context"] == context and saved["appointment_cards"] == [CARD]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_another_account_cannot_reach_someone_elses_conversation(monkeypatch):
    setup(monkeypatch, user_id="stranger")

    class Repository:
        async def get(self, owner_id, conversation_id):
            return None if owner_id != "renter" else {"messages": []}

    async def fail(self, *args, **kwargs):
        raise AssertionError("nothing may be answered or read")

    monkeypatch.setattr(agent_ask, "get_conversation_repository", lambda: Repository())
    monkeypatch.setattr(agent_ask.AppointmentAsk, "try_answer", fail)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/agent/ask", json={"question": "Lịch hẹn của tôi hôm nay", "conversationId": "c-1"})
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_general_appointment_question_still_uses_rag(monkeypatch):
    setup(monkeypatch)
    asked = []

    async def fake_rag(self, **kwargs):
        asked.append(kwargs["question"])
        return AskResult(answer="Hướng dẫn chung", status="ANSWERED", citations=[], request_id="r")

    async def fail(self, *args, **kwargs):
        raise AssertionError("general knowledge must not reach Core")

    monkeypatch.setattr(agent_ask.AskUseCases, "ask", fake_rag)
    monkeypatch.setattr(agent_ask.AppointmentAsk, "try_answer", fail)
    monkeypatch.setattr(agent_ask.ContractAsk, "try_answer", fail)
    monkeypatch.setattr(agent_ask.ListingAsk, "try_answer", lambda *a, **k: _none())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/agent/ask", json={"question": "Đặt lịch xem nhà như thế nào?"})
        assert response.json()["result"]["answer"] == "Hướng dẫn chung" and len(asked) == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_contract_question_is_not_captured_by_the_appointment_module(monkeypatch):
    setup(monkeypatch)
    reached = []

    async def fake_contract(self, question, history, request_id):
        reached.append("contract")
        return AskResult(answer="Hợp đồng ...", status="ANSWERED", citations=[], request_id=request_id,
                         data_sources=["CORE_CONTRACT"])

    async def fail(self, *args, **kwargs):
        raise AssertionError("appointment module must stay out")

    monkeypatch.setattr(agent_ask.AppointmentAsk, "try_answer", fail)
    monkeypatch.setattr(agent_ask.ContractAsk, "try_answer", fake_contract)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/agent/ask", json={"question": "Hợp đồng của tôi đã ký đủ chưa?"})
        assert reached == ["contract"] and response.json()["result"]["dataSources"] == ["CORE_CONTRACT"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_guest_personal_appointment_question_is_blocked_before_core_and_rag(monkeypatch):
    class Gateway:
        async def request(self, *args, **kwargs):
            raise AssertionError("guest must never reach Core")

        async def aclose(self):
            pass

    async def fail_rag(self, **kwargs):
        raise AssertionError("no RAG fallback for a personal question")

    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: object())
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", fail_rag)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway_client=Gateway())), headers={})
    user = UserContext(**{"X-User-Id": "guest:abc", "X-User-Role": "USER"})
    from homespace_ai.core.config import get_settings
    result = await agent_ask.generate_answer("Hôm nay tôi có lịch xem nhà nào?", [], request, user, None, get_settings(), None)
    assert result.status == "LOGIN_REQUIRED" and result.appointment_cards == []


def test_stored_cards_survive_history_serialisation_and_old_messages_still_load():
    message = ConversationMessage.model_validate({
        "id": "m", "role": "assistant", "content": "x", "status": "ANSWERED", "createdAt": "2026-10-16T00:00:00Z",
        "appointmentContext": {"appointmentIds": [A1]}, "appointmentCards": [CARD], "dataSources": ["CORE_APPOINTMENT"]})
    assert message.appointmentCards[0].roleLabel == "Người đi xem" and message.dataSources == ["CORE_APPOINTMENT"]
    assert not hasattr(message, "appointmentContext")  # references stay server-side
    legacy = ConversationMessage.model_validate({"id": "m", "role": "assistant", "content": "x",
                                                 "createdAt": "2026-10-16T00:00:00Z"})
    assert legacy.appointmentCards == [] and legacy.contractCards == [] and legacy.listingIds == []
