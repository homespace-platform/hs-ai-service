from httpx import ASGITransport, AsyncClient, Response
import pytest

from homespace_ai.api.v1 import agent_ask
from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.core.database import get_db
from homespace_ai.core.security import UserContext, get_current_user
from homespace_ai.main import app
from homespace_ai.application.listing_context import context_for
from homespace_ai.clients.generative import ListingGeneratedAnswer
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner
from homespace_ai.core.config import Settings
from tests.unit.listing_fakes import CATALOG, c, search


@pytest.mark.asyncio
async def test_agent_ask_routes_listing_to_tool_without_rag(monkeypatch):
    async def fake_db():
        yield None

    async def fake_listing_answer(self, question, history, request_id, state=None):
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
async def test_real_listing_pipeline_search_detail_recommend_and_resume_when_nearby_disabled(monkeypatch):
    """Exercise routing, native tool validation, Core HTTP tools and saved selection together.

    Only external boundaries (model transport, Core HTTP and persistence) are replaced.
    """
    ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 1001)]
    calls, plans, generations = [], [], []
    settings = Settings(_env_file=None, GENERATION_PROVIDER="openai", OPENAI_API_KEY="test-only-key")
    settings.nearby_query_enabled = False

    class Repository:
        def __init__(self):
            self.messages = []

        async def get(self, owner_id, conversation_id):
            assert owner_id == "tenant"
            return {"messages": list(self.messages)}

        async def append_message(self, owner_id, conversation_id, **message):
            self.messages.append({**message, "listingContext": message.get("listing_context")})
            return True

    class Gateway:
        async def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            if path.endswith("/catalog"):
                result = CATALOG
            elif path.endswith("/search"):
                body = kwargs["json"]
                assert body["version"] == 2 and body["filters"] == []
                result = {"version": 2, "listingIds": ids[:body["limit"]], "total": 1000, "unknownTotal": 0,
                          "readAt": "2026-10-29T03:00:00Z", "catalogVersion": CATALOG["catalogVersion"],
                          "contextMissing": [], "evidence": []}
            else:
                listing_id = path.rsplit("/", 1)[-1]
                assert listing_id in ids[:10], "Never read all 1,000 ward listings"
                result = {"id": listing_id, "title": "Phòng có WC riêng", "category": "ROOM",
                          "active": True, "status": "PUBLISHED", "areaM2": 25,
                          "pricing": {"amount": 3000000, "unit": "ROOM_MONTH"},
                          "roomDetail": {"restroomType": "PRIVATE", "maxOccupants": 2}}
            return Response(200, json={"result": result})

    class Generator:
        async def generate_listing_answer(self, *, question, evidence):
            generations.append(evidence)
            return ListingGeneratedAnswer("Phòng phù hợp theo giá thuê và thông tin trong tin đăng.",
                                          [item["id"] for item in evidence["listings"]])

    async def planner_transport(self, payload):
        plans.append(payload)
        assert {tool["name"] for tool in payload["tools"]} == {
            "search_listings", "ask_clarification", "get_listing_detail", "get_listing_details"}
        import json
        plan = search(c("category", "eq", "ROOM"), c("price_amount", "lte", "4000000"), c("room.restroom_type", "eq", "PRIVATE"),
                          ward="Phường Gò Vấp", city="Thành phố Hồ Chí Minh", sort="newest", limit=5, context={"occupants": 2})
        return {"status": "completed", "output": [{"type": "function_call", "name": "search_listings",
            "arguments": json.dumps(plan.arguments)}]}

    async def fake_db():
        yield None

    async def fail_rag(self, **kwargs):
        pytest.fail("A listing conversation must not be answered from static RAG")

    repository = Repository()
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_embedder] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: UserContext(**{"X-User-Id": "tenant", "X-User-Role": "USER"})
    app.dependency_overrides[agent_ask.get_settings] = lambda: settings
    monkeypatch.setattr(app.state, "gateway_client", Gateway(), raising=False)
    monkeypatch.setattr(agent_ask, "get_conversation_repository", lambda: repository)
    monkeypatch.setattr(agent_ask, "get_generative_client", lambda settings: Generator())
    monkeypatch.setattr(ListingToolPlanner, "_post", planner_transport)
    monkeypatch.setattr(agent_ask.AskUseCases, "ask", fail_rag)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def ask(question):
                response = await client.post("/agent/ask", json={"question": question, "conversationId": "conv-1"})
                assert response.status_code == 200
                result = response.json()["result"]
                assert result["status"] == "ANSWERED"
                return result

            found = await ask("Tìm 10 phòng trọ cho 2 người ở Phường Gò Vấp, Thành phố Hồ Chí Minh, tối đa 4 triệu, WC riêng")
            assert found["listingIds"] == ids[:10]
            searches = [body for method, path, body in calls if path.endswith("/search")]
            assert searches[0]["limit"] == 10
            for expected in [
                {"field": "ward_id", "op": "eq", "value": "24588"},
                {"field": "city_id", "op": "eq", "value": "12"},
                {"field": "category", "op": "eq", "value": "ROOM"},
                {"field": "rent.monthly_amount", "op": "lte", "value": "4000000"},
                {"field": "room.restroom_type", "op": "eq", "value": "PRIVATE"},
            ]:
                assert expected in searches[0]["where"]["all"]
            assert repository.messages[-1]["listingContext"]["searchState"]["location"]["wardId"] == "24588"
            reads = [path for method, path, body in calls if method == "GET" and not path.endswith("/catalog")]
            assert len(reads) == 10

            detailed = await ask("Phòng số 2 có WC riêng không?")
            assert detailed["listingIds"] == [ids[1]]
            assert detailed["listingPositions"] == {ids[1]: 2}
            assert generations[-1]["listings"][0]["roomDetail"]["restroomType"] == "PRIVATE"

            recommended = await ask("Trong các phòng vừa tìm, mình ở một người và muốn tiết kiệm, nên chọn phòng nào?")
            assert recommended["listingIds"] == ids[:10]
            assert generations[-1]["scopeOnly"] is True
            assert generations[-1]["selectionScope"][1] == {"position": 2, "listingId": ids[1]}
            before_map = (len(calls), len(plans), len(generations))

            unsupported = await ask("Quét tiện ích trong bán kính 3 km quanh các phòng đó")
            assert not unsupported["listingIds"]
            assert "chưa có dữ liệu" in unsupported["answer"]
            assert (len(calls), len(plans), len(generations)) == before_map

            resumed = await ask("Thôi, so sánh giá thuê của các phòng này giúp mình")
            assert resumed["listingIds"] == ids[:10]
            assert len(plans) == 1  # Known references do not trigger a new search or paid planner call.
            assert len([path for method, path, body in calls if path.endswith("/search")]) == len(searches)
            assert repository.messages[-1]["listingContext"]["listingIds"] == ids[:10]
            assert repository.messages[-1]["listingContext"]["searchState"]["constraints"]
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
                                  {"role": "assistant", "content": "[Phòng A](/rent/00000000-0000-0000-0000-000000000001)",
                                   "listingContext": context_for(["00000000-0000-0000-0000-000000000001"])}]}
        async def append_message(self, owner_id, conversation_id, **message):
            self.appended.append(message)
            return True

    repository = Repository()
    observed = {}
    async def fake_listing_answer(self, question, history, request_id, state=None):
        observed["history"] = history
        return AskResult(answer="Có thu phí gửi xe.", status="ANSWERED", citations=[], request_id=request_id,
                         listing_context=history[-1]["listingContext"],
                         listing_ids=["00000000-0000-0000-0000-000000000001"])

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
        assert repository.appended[-1]["listing_context"] == observed["history"][-1]["listingContext"]
        assert repository.appended[-1]["listing_ids"] == response.json()["result"]["listingIds"]
        assert response.json()["result"]["listingPositions"] == {"00000000-0000-0000-0000-000000000001": 1}
        assert "listingContext" not in response.json()["result"]  # Internal state isn't a new frontend contract.
    finally:
        app.dependency_overrides.clear()
