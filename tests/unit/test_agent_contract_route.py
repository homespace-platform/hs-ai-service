from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from homespace_ai.api.v1 import agent_ask
from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.api.v1.schemas import ConversationMessage
from homespace_ai.application.contract_context import make_context
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.core.database import get_db
from homespace_ai.core.security import UserContext, get_current_user
from homespace_ai.main import app

C1 = "00000000-0000-0000-0000-000000000001"
CARD = {"contractId": C1, "contractNumber": "HD-DEMO-001", "property": "1 Đường Mẫu", "askerRole": "TENANT",
        "roleLabel": "Người thuê", "status": "ACTIVE", "statusLabel": "Đang có hiệu lực", "leasePhase": "IN_LEASE",
        "phaseLabel": "Đang trong thời hạn thuê", "leaseEnd" "Exclusive": "2027-01-16"}


async def fake_db():
    yield None


def setup(monkeypatch, user_id="tenant"):
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_embedder] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: UserContext(**{"X-User-Id": user_id, "X-User-Role": "USER"})
    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: object())


@pytest.mark.asyncio
async def test_personal_contract_question_skips_rag_and_persists_references_and_cards(monkeypatch):
    setup(monkeypatch)
    context = make_context([C1], ["HD-DEMO-001"])
    appended = []

    class Repository:
        async def get(self, owner_id, conversation_id):
            assert owner_id == "tenant"
            return {"messages": [{"role": "assistant", "content": "x", "contractContext": context}]}

        async def append_message(self, owner_id, conversation_id, **message):
            appended.append(message)
            return True

    seen = {}

    async def fake_contract(self, question, history, request_id):
        seen["history"] = history
        return AskResult(answer="Theo dữ liệu hệ thống ...", status="ANSWERED", citations=[], request_id=request_id,
                         contract_context=context, contract_cards=[CARD], data_sources=["CORE_CONTRACT"])

    async def fail_rag(self, **kwargs):
        raise AssertionError("A personal contract question must not be answered by the old RAG regex")

    monkeypatch.setattr(agent_ask, "get_conversation_repository", lambda: Repository())
    monkeypatch.setattr(agent_ask.ContractAsk, "try_answer", fake_contract)
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", fail_rag)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/agent/ask", json={"question": "Hợp đồng của tôi đã ký đủ chưa?",
                                                              "conversationId": "c-1"})
        body = response.json()["result"]
        assert response.status_code == 200 and body["status"] == "ANSWERED"
        assert body["contractCards"][0]["contractNumber"] == "HD-DEMO-001" and body["dataSources"] == ["CORE_CONTRACT"]
        assert "contractContext" not in body  # internal state, like listingContext
        assert seen["history"][0]["contractContext"] == context
        assert appended[-1]["contract_context"] == context and appended[-1]["contract_cards"] == [CARD]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_general_contract_question_still_uses_rag(monkeypatch):
    setup(monkeypatch)
    asked = []

    async def fake_rag(self, **kwargs):
        asked.append(kwargs["question"])
        return AskResult(answer="Hướng dẫn chung", status="ANSWERED", citations=[], request_id="r")

    async def fail_contract(self, *args, **kwargs):
        raise AssertionError("general knowledge must not reach Core")

    monkeypatch.setattr(agent_ask.AskUseCases, "ask", fake_rag)
    monkeypatch.setattr(agent_ask.ContractAsk, "try_answer", fail_contract)
    monkeypatch.setattr(agent_ask.ListingAsk, "try_answer", lambda *a, **k: _none())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/agent/ask", json={"question": "Ký hợp đồng trên HomeSpace như thế nào?"})
        assert response.json()["result"]["answer"] == "Hướng dẫn chung" and len(asked) == 1
    finally:
        app.dependency_overrides.clear()


async def _none():
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("with_contract_history", [False, True])
async def test_renter_search_with_existing_furniture_never_calls_personal_contract_or_rag(monkeypatch, with_contract_history):
    from homespace_ai.core.config import get_settings
    question = ("Mình cần thuê phòng trọ cho 2 người ở phường Gò Vấp, TP.HCM, tiền thuê dưới 3,5 triệu/tháng. "
                "Bọn mình đã có giường tủ nên ưu tiên phòng rộng, nội thất không quan trọng. Cho mình xem 4 phòng mới đăng gần đây.")
    history = [{"role": "assistant", "content": "Hợp đồng của bạn", "contractContext": make_context([C1])}] if with_contract_history else []
    seen = []

    async def listing(self, text, messages, request_id, **kwargs):
        seen.append(text)
        return AskResult(answer="Bảng 4 phòng", status="ANSWERED", citations=[], request_id=request_id)

    async def fail(self, *args, **kwargs):
        raise AssertionError("A new rental search must not query personal contracts, appointments or RAG")

    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: object())
    monkeypatch.setattr(agent_ask.ListingAsk, "try_answer", listing)
    monkeypatch.setattr(agent_ask.ContractAsk, "try_answer", fail)
    monkeypatch.setattr(agent_ask.AppointmentAsk, "try_answer", fail)
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", fail)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway_client=object())), headers={})
    user = UserContext(**{"X-User-Id": "tenant", "X-User-Role": "USER"})
    result = await agent_ask.generate_answer(question, history, request, user, None, get_settings(), None)
    assert result.answer == "Bảng 4 phòng" and seen == [question]


@pytest.mark.asyncio
async def test_guest_personal_contract_question_is_blocked_before_core_and_rag(monkeypatch):
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
    result = await agent_ask.generate_answer("Hợp đồng của tôi đã ký đủ chưa?", [], request, user, None,
                                             get_settings(), None)
    assert result.status == "LOGIN_REQUIRED" and result.contract_cards == []


def test_stored_cards_and_sources_survive_history_serialisation():
    message = ConversationMessage.model_validate({
        "id": "m", "role": "assistant", "content": "x", "status": "ANSWERED", "createdAt": "2026-10-16T00:00:00Z",
        "contractContext": {"contractIds": [C1]}, "contractCards": [CARD], "dataSources": ["CORE_CONTRACT", "RAG"]})
    assert message.contractCards[0].roleLabel == "Người thuê" and message.dataSources == ["CORE_CONTRACT", "RAG"]
    assert not hasattr(message, "contractContext")  # references stay server-side
    legacy = ConversationMessage.model_validate({"id": "m", "role": "assistant", "content": "x",
                                                 "createdAt": "2026-10-16T00:00:00Z"})
    assert legacy.contractCards == [] and legacy.listingIds == []
