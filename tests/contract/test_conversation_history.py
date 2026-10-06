import base64
import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from homespace_ai.api.v1 import agent_ask
from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.core.config import get_settings
from homespace_ai.core.database import get_db
from homespace_ai.main import app
from homespace_ai.repositories.conversation_repo import (
    ConversationRepository,
    get_conversation_repository,
)


@pytest.mark.asyncio
async def test_mongo_history_is_private_persistent_and_deletable(monkeypatch):
    if os.getenv("HS_RUN_MONGO_INTEGRATION") != "1":
        pytest.skip("Set HS_RUN_MONGO_INTEGRATION=1 to test the existing MongoDB container.")

    settings = get_settings()
    repository = ConversationRepository(settings.ai_mongodb_uri)
    await repository.ensure_indexes()
    app.dependency_overrides[get_conversation_repository] = lambda: repository
    monkeypatch.setattr(agent_ask, "get_conversation_repository", lambda: repository)

    async def fake_db():
        yield None

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_embedder] = lambda: None
    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: None)

    owner = f"history-test-{uuid4()}"
    other = f"history-other-{uuid4()}"

    def headers(user_id: str):
        return {
            "X-User-Id": user_id,
            "X-User-Role": "USER",
            "X-User-Name-B64": base64.b64encode("Khách thử nghiệm".encode()).decode(),
            "X-Internal-Secret": settings.gateway_internal_secret,
        }

    conversation_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/agent/conversations", headers=headers(owner))
            assert created.status_code == 201, created.text
            conversation_id = created.json()["result"]["id"]

            reply = await client.post(
                "/agent/ask",
                json={"question": "Chào bạn", "conversationId": conversation_id},
                headers=headers(owner),
            )
            assert reply.status_code == 200, reply.text
            assert "Khách thử nghiệm" in reply.json()["result"]["answer"]

            async def fake_answer(self, question, **kwargs):
                return AskResult(answer="Mình có thể giúp bạn.", status="ANSWERED", citations=[], request_id="test-answer")

            monkeypatch.setattr(agent_ask.AskUseCases, "ask", fake_answer)
            answered = await client.post(
                "/agent/ask",
                json={"question": "HomeSpace giúp gì?", "conversationId": conversation_id},
                headers=headers(owner),
            )
            assert answered.status_code == 200, answered.text

            history = await client.get(f"/agent/conversations/{conversation_id}", headers=headers(owner))
            assert history.status_code == 200
            messages = history.json()["result"]["messages"]
            assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
            assert messages[0]["content"] == "Chào bạn"
            assert messages[-1]["content"] == "Mình có thể giúp bạn."
            assert history.json()["result"]["title"] == "Chào bạn"

            reopened = ConversationRepository(settings.ai_mongodb_uri)
            try:
                assert len((await reopened.get(owner, conversation_id))["messages"]) == 4
            finally:
                await reopened.close()

            listed = await client.get("/agent/conversations", headers=headers(owner))
            assert any(item["id"] == conversation_id for item in listed.json()["result"])
            assert all(item["id"] != conversation_id for item in (await client.get("/agent/conversations", headers=headers(other))).json()["result"])
            assert (await client.get(f"/agent/conversations/{conversation_id}", headers=headers(other))).status_code == 404
            assert (await client.delete(f"/agent/conversations/{conversation_id}", headers=headers(other))).status_code == 404
            assert (await client.post(
                "/agent/ask", json={"question": "Chào bạn", "conversationId": conversation_id},
                headers=headers(other),
            )).status_code == 404

            pinned = await client.patch(
                f"/agent/conversations/{conversation_id}/pin",
                json={"isPinned": True}, headers=headers(owner),
            )
            assert pinned.json()["result"]["isPinned"] is True

            deleted = await client.delete(f"/agent/conversations/{conversation_id}", headers=headers(owner))
            assert deleted.status_code == 204
            assert (await client.get(f"/agent/conversations/{conversation_id}", headers=headers(owner))).status_code == 404
    finally:
        if conversation_id:
            await repository.delete(owner, conversation_id)
        app.dependency_overrides.clear()
        await repository.close()
