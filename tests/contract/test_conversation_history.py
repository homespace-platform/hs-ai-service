import base64
import os
from uuid import uuid4
from unittest.mock import AsyncMock
import hashlib

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


@pytest.mark.asyncio
async def test_real_mongo_bulk_delete_removes_messages_memory_and_pins_only_for_current_owner():
    if os.getenv("HS_RUN_MONGO_INTEGRATION") != "1":
        pytest.skip("Set HS_RUN_MONGO_INTEGRATION=1 for real Mongo verification.")
    settings = get_settings()
    repository = ConversationRepository(settings.ai_mongodb_uri)
    owner, other = "bulk-history-test-" + str(uuid4()), "bulk-history-other-" + str(uuid4())
    ids = []
    app.dependency_overrides[get_conversation_repository] = lambda: repository
    def headers(user_id):
        return {"X-User-Id": user_id, "X-Internal-Secret": settings.gateway_internal_secret}
    try:
        for user in (owner, owner, other):
            document = await repository.create(user)
            ids.append(document["_id"])
            await repository.append_message(user, document["_id"], role="user", content="Tìm phòng thử nghiệm")
            await repository.append_message(user, document["_id"], role="assistant", content="Nội dung thử nghiệm",
                listing_context={"searchState": {"revision": 1, "test": True}}, listing_ids=["test-listing"])
        await repository.set_pinned(owner, ids[0], True)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete("/agent/conversations", headers=headers(owner))
            assert response.status_code == 204, response.text
            assert (await client.get("/agent/conversations", headers=headers(owner))).json()["result"] == []
            for identifier in ids[:2]:
                assert (await client.get(f"/agent/conversations/{identifier}", headers=headers(owner))).status_code == 404
            assert await repository.collection.count_documents({"ownerId": owner}) == 0
            assert await repository.collection.count_documents({"_id": {"$in": ids[:2]}}) == 0
            assert len((await repository.get(other, ids[2]))["messages"]) == 2
            assert (await client.delete("/agent/conversations", headers=headers(owner))).status_code == 204
        # An in-flight generation cannot recreate a deleted conversation or restore its memory.
        assert not await repository.append_message(owner, ids[0], role="assistant", content="Late response")
        assert await repository.collection.count_documents({"ownerId": owner}) == 0
        reopened = ConversationRepository(settings.ai_mongodb_uri)
        try:
            assert await reopened.list_for_user(owner) == []
            assert await reopened.get(other, ids[2]) is not None
        finally:
            await reopened.close()
    finally:
        # Remove only UUID-tagged test documents, never existing account histories.
        await repository.collection.delete_many({"_id": {"$in": ids}, "ownerId": {"$in": [owner, other]}})
        app.dependency_overrides.pop(get_conversation_repository, None)
        await repository.close()


@pytest.mark.asyncio
async def test_real_mongo_guest_metadata_only_three_turns_and_account_claim(monkeypatch):
    if os.getenv("HS_RUN_MONGO_INTEGRATION") != "1":
        pytest.skip("Set HS_RUN_MONGO_INTEGRATION=1 for local Mongo verification.")
    settings = get_settings()
    repository = ConversationRepository(settings.ai_mongodb_uri)
    account = "guest-claim-test-" + str(uuid4())
    identifier = None
    guest = None
    try:
        await repository.ensure_indexes()
        monkeypatch.setattr(repository, "consume_guest_quota", AsyncMock(return_value=0))
        app.dependency_overrides[get_conversation_repository] = lambda: repository
        async def database():
            yield None
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_embedder] = lambda: None
        monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: None)
        async def answer(self, **kwargs):
            return AskResult("Trả lời thử nghiệm", "ANSWERED", [], kwargs["request_id"],
                             listing_context={"listingIds": ["verified-test-listing"]})
        monkeypatch.setattr(agent_ask.AskUseCases, "ask", answer)
        headers = {"X-Internal-Secret": settings.gateway_internal_secret}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
            created = await c.post("/guest/agent/conversations")
            assert created.status_code == 201, created.text
            state = created.json()["result"]
            identifier = state["id"]
            guest = "guest:" + hashlib.sha256(c.cookies["hs_ai_guest"].encode()).hexdigest()
            stale = state["guestState"]
            for remaining in (2, 1, 0):
                response = await c.post("/guest/agent/ask", json={"question": "Câu hỏi thử nghiệm",
                    "conversationId": identifier, "guestState": state["guestState"]})
                assert response.status_code == 200, response.text
                state.update(response.json()["result"])
                assert state["remainingQuestions"] == remaining
            assert await repository.collection.count_documents({"_id": identifier}) == 0
            trial = await repository.guest_limits.find_one({"_id": "trial:" + guest})
            assert trial["count"] == 3 and "messages" not in trial
            replay = await c.post("/guest/agent/ask", json={"question": "Replay",
                "conversationId": identifier, "guestState": stale})
            assert replay.status_code == 409
            denied = await c.post("/guest/agent/ask", json={"question": "Câu thứ tư",
                "conversationId": identifier, "guestState": state["guestState"]})
            assert denied.status_code == 403
            for _ in range(2):
                claimed = await c.post("/agent/conversations/claim-guest", json={"guestState": state["guestState"]},
                                       headers={"X-User-Id": account})
                assert claimed.status_code == 200, claimed.text
                assert len(claimed.json()["result"]["messages"]) == 6
            assert await repository.collection.count_documents({"_id": identifier}) == 1
            persisted = await repository.get(account, identifier)
            assert persisted["messages"][-1]["listingContext"]["listingIds"] == ["verified-test-listing"]
            assert "expiresAt" not in persisted
    finally:
        # Exact UUID/owner test artifacts only; never delete user conversations.
        if identifier:
            await repository.collection.delete_one({"_id": identifier, "ownerId": account})
        if guest:
            await repository.guest_limits.delete_one({"_id": "trial:" + guest})
        app.dependency_overrides.clear()
        await repository.close()
