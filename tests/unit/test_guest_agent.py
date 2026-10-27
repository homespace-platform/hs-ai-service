from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4
import asyncio

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from homespace_ai.api.v1 import agent_ask
from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.core.config import get_settings
from homespace_ai.core.database import get_db
from homespace_ai.core.guest_state import decode_state, encode_state
from homespace_ai.main import app
from homespace_ai.repositories.conversation_repo import ConversationRepository, get_conversation_repository


class MemoryRepository:
    def __init__(self):
        self.documents = {}
        self.trials = {}
        self.consume_guest_quota = AsyncMock(return_value=0)

    async def start_guest(self, owner, identifier):
        trial = self.trials.setdefault(owner, {"count": 0})
        trial["activeSession"] = identifier
        return deepcopy(trial)

    async def reserve_guest_question(self, owner, identifier, count):
        trial = self.trials[owner]
        if trial["activeSession"] != identifier or trial["count"] != count or count >= 3 or "claimedOwner" in trial:
            return None
        trial["count"] += 1
        return deepcopy(trial)

    async def release_guest_question(self, owner, identifier, count):
        trial = self.trials[owner]
        if trial["activeSession"] == identifier and trial["count"] == count + 1:
            trial["count"] -= 1

    async def claim_guest(self, state, account):
        trial = self.trials[state["owner"]]
        if trial["activeSession"] != state["id"] or trial["count"] != state["count"] or trial.get("claimedOwner", account) != account:
            return None
        trial["claimedOwner"] = account
        now = datetime.now(timezone.utc)
        return deepcopy(self.documents.setdefault(state["id"], dict(
            _id=state["id"], ownerId=account, title=state["messages"][0]["content"][:60],
            isPinned=False, createdAt=now, updatedAt=now, messages=state["messages"])))

    async def get(self, owner, identifier):
        doc = self.documents.get(identifier)
        return deepcopy(doc) if doc and doc["ownerId"] == owner else None


@pytest.fixture
def guest_repo(monkeypatch):
    repo = MemoryRepository()
    async def database():
        yield None
    app.dependency_overrides[get_conversation_repository] = lambda: repo
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_embedder] = lambda: None
    monkeypatch.setattr(agent_ask, "get_conversation_repository", lambda: repo)
    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: None)
    async def rag(self, **kwargs):
        return AskResult(answer="Hướng dẫn công khai", status="ANSWERED", citations=[], request_id=kwargs["request_id"])
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", rag)
    yield repo
    app.dependency_overrides.clear()


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                            headers={"X-Internal-Secret": get_settings().gateway_internal_secret})


async def start(c):
    response = await c.post("/guest/agent/conversations")
    assert response.status_code == 201, response.text
    return response.json()["result"]


async def ask(c, state, question="HomeSpace là gì?", **kwargs):
    return await c.post("/guest/agent/ask", json={"question": question, "conversationId": state["id"],
                                                "guestState": state["guestState"]}, **kwargs)


@pytest.mark.asyncio
async def test_guest_three_questions_no_saved_history_and_fourth_no_generation(guest_repo, monkeypatch):
    called = AsyncMock(side_effect=lambda **kw: AskResult("Công khai", "ANSWERED", [], kw["request_id"]))
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", called)
    async with client() as c:
        initial = await c.get("/guest/agent/conversations")
        cookie = initial.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age" not in cookie
        assert initial.headers["cache-control"] == "no-store"
        state = await start(c)
        for remaining in (2, 1, 0):
            response = await ask(c, state)
            assert response.status_code == 200, response.text
            state.update(response.json()["result"])
            assert state["remainingQuestions"] == remaining
        denied = await ask(c, state)
        assert denied.status_code == 403 and denied.json()["detail"]["code"] == "GUEST_LOGIN_REQUIRED"
        assert called.await_count == 3
        assert guest_repo.documents == {}
        assert (await c.get("/guest/agent/conversations")).json()["result"] == []
        assert (await c.get(f'/guest/agent/conversations/{state["id"]}')).status_code == 404
        fresh = await start(c)
        assert fresh["remainingQuestions"] == 0  # Refresh/new chat never resets the trial.
        assert (await ask(c, fresh)).status_code == 403


@pytest.mark.asyncio
async def test_guest_rag_is_public_ignores_forged_admin(guest_repo, monkeypatch):
    called = AsyncMock(return_value=AskResult("Public", "ANSWERED", [], "request"))
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", called)
    async with client() as c:
        state = await start(c)
        response = await ask(c, state, headers={"X-User-Id": "admin", "X-User-Role": "ADMIN", "X-User-Name": "Spoof"})
        assert response.status_code == 200
        assert called.call_args.kwargs["user_role"] == "USER"
        assert called.call_args.kwargs["user_name"] is None
        assert (await c.post("/agent/ask", json={"question": "Tìm phòng"})).status_code == 401
        assert (await c.get("/admin/knowledge/documents")).status_code == 401


@pytest.mark.asyncio
async def test_guest_signed_followup_tool_context_and_login_claim(guest_repo, monkeypatch):
    observed = []
    listing_id = str(uuid4())
    async def listing(self, question, history, request_id):
        observed.append(deepcopy(history))
        return AskResult(answer="Tin công khai", status="ANSWERED", citations=[], request_id=request_id,
                         listing_ids=[listing_id], listing_context={"listingIds": [listing_id], "displayedIds": [listing_id]})
    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: object())
    monkeypatch.setattr(agent_ask.ListingAsk, "try_answer", listing)
    async with client() as c, client() as other:
        state = await start(c)
        for question in ["Tìm trọ Phú Lợi", "Phòng đó ở mấy người?"]:
            response = await ask(c, state, question)
            assert response.status_code == 200
            state.update(response.json()["result"])
        assert observed[1][-1]["listingContext"]["displayedIds"] == [listing_id]
        assert guest_repo.documents == {}
        claim = "/agent/conversations/claim-guest"
        body = {"guestState": state["guestState"]}
        assert (await c.post(claim, json=body)).status_code == 401
        assert (await other.post(claim, json=body, headers={"X-User-Id": "tenant"})).status_code == 403
        response = await c.post(claim, json=body, headers={"X-User-Id": "tenant"})
        assert response.status_code == 200, response.text
        detail = response.json()["result"]
        assert len(detail["messages"]) == 4 and detail["messages"][-1]["listingIds"] == [listing_id]
        assert "listingContext" not in detail["messages"][-1]  # Internal trusted state stays server-side after claim.
        assert guest_repo.documents[state["id"]]["ownerId"] == "tenant"
        assert guest_repo.documents[state["id"]]["messages"][-1]["listingContext"]["displayedIds"] == [listing_id]
        assert (await c.post(claim, json=body, headers={"X-User-Id": "tenant"})).json()["result"]["id"] == detail["id"]
        assert len(guest_repo.documents) == 1
        assert (await c.post(claim, json=body, headers={"X-User-Id": "another"})).status_code == 409
        assert (await c.get(f'/agent/conversations/{detail["id"]}', headers={"X-User-Id": "another"})).status_code == 404


@pytest.mark.asyncio
async def test_forged_foreign_expired_and_replayed_states_block_before_model(guest_repo, monkeypatch):
    calls = AsyncMock(return_value=AskResult("Public", "ANSWERED", [], "r"))
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", calls)
    async with client() as c, client() as other:
        state = await start(c)
        assert (await ask(other, state)).status_code == 403
        forged = {**state, "guestState": "x" + state["guestState"][1:]}
        assert (await ask(c, forged)).status_code == 403
        assert calls.await_count == 0
        results = await asyncio.gather(ask(c, state), ask(c, state))
        assert sorted(r.status_code for r in results) == [200, 409]
        assert calls.await_count == 1
        assert (await ask(c, state)).status_code == 409
        decoded = decode_state(state["guestState"], next(iter(guest_repo.trials)), get_settings())
        monkeypatch.setattr("homespace_ai.core.guest_state.time.time", lambda: decoded["expires"] + 1)
        assert (await ask(c, state)).status_code == 403


@pytest.mark.asyncio
async def test_generation_exception_refunds_turn(guest_repo, monkeypatch):
    failed = AsyncMock(side_effect=HTTPException(503, "Unavailable"))
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", failed)
    async with client() as c:
        state = await start(c)
        assert (await ask(c, state)).status_code == 503
        assert next(iter(guest_repo.trials.values()))["count"] == 0
        failed.side_effect = None
        failed.return_value = AskResult("Okay", "ANSWERED", [], "r")
        assert (await ask(c, state)).status_code == 200


@pytest.mark.asyncio
async def test_guest_requires_gateway_trust_quota_and_signing_key(guest_repo, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/guest/agent/conversations")).status_code == 403
    guest_repo.consume_guest_quota.return_value = 60
    async with client() as c:
        response = await c.post("/guest/agent/conversations")
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
    guest_repo.consume_guest_quota.return_value = 0
    monkeypatch.setattr(get_settings(), "gateway_internal_secret", "")
    async with client() as c:
        assert (await c.post("/guest/agent/conversations")).status_code == 503
    assert guest_repo.documents == {} and guest_repo.trials == {}


@pytest.mark.asyncio
async def test_validation_never_logs_or_echoes_guest_context(guest_repo, monkeypatch):
    from homespace_ai import main
    logged = []
    monkeypatch.setattr(main.logger, "error", lambda event, **fields: logged.append(fields))
    async with client() as c:
        state = await start(c)
        response = await c.post("/guest/agent/ask", json={"question": "private-prompt" * 100,
            "conversationId": state["id"], "guestState": state["guestState"]})
        assert response.status_code == 422
        assert "private-prompt" not in response.text and state["guestState"] not in response.text
        assert "body" not in logged[-1]
        assert all("input" not in error and "ctx" not in error for error in logged[-1]["errors"])


@pytest.mark.asyncio
async def test_guest_cookie_secure_outside_local(guest_repo, monkeypatch):
    monkeypatch.setattr(get_settings(), "app_env", "production")
    async with client() as c:
        response = await c.get("/guest/agent/conversations", headers={"Cookie": "hs_ai_guest=admin"})
        assert "Secure" in response.headers["set-cookie"] and "admin" not in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_repository_refuses_guest_history_and_atomic_budget_metadata():
    repo = ConversationRepository.__new__(ConversationRepository)
    repo.collection = AsyncMock()
    repo.guest_limits = AsyncMock()
    with pytest.raises(ValueError):
        await repo.create("guest:hash")
    account = await repo.create("account")
    assert "expiresAt" not in account
    await repo.start_guest("guest:hash", "session")
    await repo.reserve_guest_question("guest:hash", "session", 2)
    query, operation = repo.guest_limits.find_one_and_update.call_args.args
    assert query["count"] == {"$eq": 2, "$lt": 3}
    assert operation == {"$inc": {"count": 1}}
    for call in repo.guest_limits.mock_calls:
        assert "messages" not in str(call) and "content" not in str(call)


@pytest.mark.asyncio
async def test_claim_receipt_atomic_and_account_insert_idempotent():
    repo = ConversationRepository.__new__(ConversationRepository)
    repo.collection = AsyncMock()
    repo.guest_limits = AsyncMock()
    state = {"owner": "guest:hash", "id": str(uuid4()), "count": 3,
             "messages": [{"role": "user", "content": "Question"}]}
    await repo.claim_guest(state, "tenant")
    query, operation = repo.guest_limits.find_one_and_update.call_args.args
    assert query["count"] == 3 and operation == {"$set": {"claimedOwner": "tenant"}}
    query, operation = repo.collection.find_one_and_update.call_args.args
    assert query["ownerId"] == "tenant" and "$setOnInsert" in operation
    repo.guest_limits.find_one_and_update.return_value = None
    repo.collection.reset_mock()
    assert await repo.claim_guest(state, "another") is None
    repo.collection.find_one_and_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_quota_atomic_shared_returns_retry_after():
    repo = ConversationRepository.__new__(ConversationRepository)
    repo.guest_limits = AsyncMock()
    repo.guest_limits.find_one_and_update.side_effect = [{"count": 1}, {"count": 1}, {"count": 999}]
    assert await repo.consume_guest_quota("ip-hash") == 0
    assert await repo.consume_guest_quota("ip-hash") > 0


@pytest.mark.asyncio
async def test_anonymous_downstream_only_public_listing_tools():
    gateway = GatewayApiClient("http://gateway", 2)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"result": {}})
    await gateway._client.aclose()
    gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    request = Request({"type": "http", "headers": []})
    try:
        await gateway.request("POST", "/api/v1/public/listings/ai/search", request=request, json={})
        assert "authorization" not in calls[0].headers and "cookie" not in calls[0].headers
        with pytest.raises(HTTPException) as error:
            await gateway.request("GET", "/api/v1/contracts", request=request)
        assert error.value.status_code == 401 and len(calls) == 1
    finally:
        await gateway.aclose()
