from httpx import ASGITransport, AsyncClient
import pytest

from homespace_ai.api.v1 import agent_ask
from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.core.database import get_db
from homespace_ai.core.security import UserContext, get_current_user
from homespace_ai.main import app


@pytest.mark.asyncio
async def test_agent_ask_routes_listing_to_tool_without_rag(monkeypatch):
    async def fake_db():
        yield None

    async def fake_listing_answer(self, question, history, request_id):
        return AskResult(answer="Dữ liệu từ Core API", status="ANSWERED", citations=[], request_id=request_id)

    async def fail_rag(self, **kwargs):
        raise AssertionError("Listing question must not fall through to RAG")

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_embedder] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: UserContext(**{"X-User-Id": "tenant", "X-User-Role": "USER"})
    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: object())
    monkeypatch.setattr(agent_ask.ListingAsk, "try_answer", fake_listing_answer)
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", fail_rag)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/agent/ask", json={"question": "Tìm phòng trọ có gửi xe"})
        assert response.status_code == 200
        assert response.json()["result"]["answer"] == "Dữ liệu từ Core API"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_listing_followup_receives_previous_conversation_messages(monkeypatch):
    async def fake_db():
        yield None

    class Repository:
        def __init__(self):
            self.appended = []
        async def get(self, owner_id, conversation_id):
            assert owner_id == "tenant"
            return {"messages": [{"role": "user", "content": "Tìm phòng trọ"},
                                  {"role": "assistant", "content": "[Phòng A](/rent/00000000-0000-0000-0000-000000000001)"}]}
        async def append_message(self, owner_id, conversation_id, **message):
            self.appended.append(message)
            return True

    repository = Repository()
    observed = {}
    async def fake_listing_answer(self, question, history, request_id):
        observed["history"] = history
        return AskResult(answer="Có thu phí gửi xe.", status="ANSWERED", citations=[], request_id=request_id)

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_embedder] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: UserContext(**{"X-User-Id": "tenant", "X-User-Role": "USER"})
    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: object())
    monkeypatch.setattr(agent_ask, "get_conversation_repository", lambda: repository)
    monkeypatch.setattr(agent_ask.ListingAsk, "try_answer", fake_listing_answer)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/agent/ask", json={"question": "Còn chỗ gửi xe?", "conversationId": "conv-1"})
        assert response.status_code == 200
        assert len(observed["history"]) == 2
        assert observed["history"][1]["role"] == "assistant"
        assert [message["role"] for message in repository.appended] == ["user", "assistant"]
    finally:
        app.dependency_overrides.clear()
