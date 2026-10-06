from types import SimpleNamespace

import pytest

from homespace_ai.api.v1.agent_ask import ask_agent
from homespace_ai.api.v1.schemas import AskRequest
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.core.security import UserContext
from homespace_ai.property_search.routing import (
    QuestionRoute,
    knowledge_history,
    listing_user_history,
    route_question,
)
from homespace_ai.property_search.intent import SearchIntent


def _route(question: str) -> QuestionRoute:
    return route_question(
        question,
        has_search_context=True,
        has_previous_results=True,
    )


@pytest.mark.parametrize("question", [
    "HomeSpace là gì và có những tính năng nào?",
    "Chủ nhà đăng tin cho thuê trên HomeSpace như thế nào?",
    "Cách đăng tin cho thuê nhà là gì?",
    "Chính sách kiểm duyệt tin đăng của HomeSpace ra sao?",
    "Làm sao để đặt lịch xem nhà trên HomeSpace?",
])
def test_platform_help_overrides_saved_property_search(question):
    assert _route(question) == QuestionRoute.KNOWLEDGE


@pytest.mark.parametrize("question", [
    "Chủ nhà của phòng này là ai?",
    "Hai tin vừa gửi có cho đặt lịch xem nhà không?",
    "Giá thuê căn hộ này thanh toán theo kỳ nào?",
    "Trong hai phòng đó, phòng nào gửi xe miễn phí?",
])
def test_specific_listing_questions_keep_live_facts_route(question):
    assert _route(question) == QuestionRoute.LISTING_DETAIL


@pytest.mark.parametrize("question", [
    "Tìm căn hộ ở Gò Vấp dưới 16 triệu?",
    "Tìm phòng trọ có ban công dưới 3 triệu",
    "Bỏ yêu cầu ban công, tìm thêm phòng khác",
])
def test_new_search_still_uses_listing_tool(question):
    assert _route(question) == QuestionRoute.LISTING_SEARCH


def test_unrelated_question_is_not_forced_into_saved_search():
    assert _route("Xin chào") == QuestionRoute.KNOWLEDGE
    assert _route("Hôm nay bạn thế nào?") == QuestionRoute.KNOWLEDGE
    assert route_question(
        "HomeSpace là gì?", has_search_context=False, has_previous_results=False,
    ) == QuestionRoute.KNOWLEDGE


def test_fresh_listing_search_can_start_after_knowledge_without_context():
    for question in (
        "Tìm phòng trọ có gác và ban công dưới 1 triệu ở Phường An Nhơn.",
        "Tìm phòng trọ có gác và ban công dưới 1 triệu ở Phường Bình Thạnh.",
    ):
        assert route_question(
            question, has_search_context=False, has_previous_results=False,
        ) == QuestionRoute.LISTING_SEARCH


def test_histories_are_separated_by_route():
    history = [
        {"role": "user", "content": "Tìm phòng ở Gò Vấp"},
        {"role": "assistant", "status": "PROPERTY_SEARCH", "content": "Tin A"},
        {"role": "user", "content": "HomeSpace là gì?"},
        {"role": "assistant", "status": "ANSWERED", "content": "HomeSpace là..."},
    ]
    assert [item["content"] for item in knowledge_history(history)] == [
        "HomeSpace là gì?", "HomeSpace là...",
    ]
    assert listing_user_history(history) == ["Tìm phòng ở Gò Vấp"]


@pytest.mark.asyncio
async def test_saved_search_context_does_not_call_mcp_for_platform_help(monkeypatch):
    class Repo:
        async def get(self, user_id, conversation_id):
            return {
                "searchContext": {"provinceCode": "79", "district": "Phường Gò Vấp"},
                "searchState": {"resultIds": ["listing-1"]},
                "messages": [
                    {"role": "user", "content": "Tìm căn hộ ở Gò Vấp"},
                    {"role": "assistant", "status": "PROPERTY_SEARCH", "content": "Tin A"},
                ],
            }

        async def append_message(self, *args, **kwargs):
            return True

    called = []

    class UseCases:
        async def ask(self, **kwargs):
            called.append(kwargs)
            return AskResult(
                answer="HomeSpace là nền tảng hỗ trợ thuê nhà.", status="ANSWERED",
                citations=[], request_id=kwargs["request_id"],
            )

    async def forbidden_mcp(*args, **kwargs):
        raise AssertionError("Platform help must not query listing MCP")

    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.get_conversation_repository", Repo)
    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.get_generative_client", lambda settings: object())
    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.AskUseCases", lambda **kwargs: UseCases())
    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.call_listing_tool", forbidden_mcp)

    user = UserContext.model_validate({"X-User-Id": "test-user", "X-User-Role": "USER"})
    for question in (
        "HomeSpace là gì và có những tính năng nào?",
        "Chủ nhà đăng tin cho thuê trên HomeSpace như thế nào?",
    ):
        response = await ask_agent(
            AskRequest(question=question, conversationId="conversation-1"),
            user=user, session=object(), settings=SimpleNamespace(), embedder=object(),
        )
        assert response.result.status == "ANSWERED"
    assert len(called) == 2
    assert all(call["conversation_history"] == [] for call in called)


@pytest.mark.asyncio
async def test_knowledge_conversation_can_switch_to_listing_search(monkeypatch):
    class Repo:
        async def get(self, user_id, conversation_id):
            return {
                "searchContext": None,
                "searchState": None,
                "messages": [
                    {"role": "user", "content": "HomeSpace là gì?"},
                    {"role": "assistant", "status": "ANSWERED", "content": "HomeSpace là..."},
                ],
            }

        async def append_message(self, *args, **kwargs):
            return True

        async def set_search_context(self, *args, **kwargs):
            return True

        async def set_search_state(self, *args, **kwargs):
            return True

    called = []

    class UseCases:
        async def ask(self, **kwargs):
            raise AssertionError("Listing search must not be sent to RAG")

    async def fake_listing_tool(name, arguments):
        called.append((name, arguments))
        if name == "resolve_listing_ward":
            return {"ward": "Phường Bình Thạnh"}
        if name == "search_listings":
            return {"matches": [], "total": 0}
        raise AssertionError(name)

    async def fake_parse_intent(question, previous_user_messages):
        assert previous_user_messages == []
        return SearchIntent(category="ROOM", price_max=1_000_000, has_mezzanine=True,
                            has_balcony=True)

    async def fake_reply(**kwargs):
        return "Mình chưa tìm thấy phòng phù hợp với tất cả tiêu chí này."

    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.get_conversation_repository", Repo)
    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.get_generative_client", lambda settings: object())
    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.AskUseCases", lambda **kwargs: UseCases())
    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.call_listing_tool", fake_listing_tool)
    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.parse_intent", fake_parse_intent)
    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.compose_search_reply", fake_reply)

    user = UserContext.model_validate({"X-User-Id": "test-user", "X-User-Role": "USER"})
    response = await ask_agent(
        AskRequest(
            question="Tìm phòng trọ có gác và ban công dưới 1 triệu ở Phường Bình Thạnh.",
            conversationId="conversation-1",
            searchContext={"provinceCode": "79"},
        ),
        user=user, session=object(), settings=SimpleNamespace(), embedder=object(),
    )
    assert response.result.status == "PROPERTY_SEARCH"
    assert called[0] == ("resolve_listing_ward", {
        "province_code": "79",
        "query": "Tìm phòng trọ có gác và ban công dưới 1 triệu ở Phường Bình Thạnh.",
    })
    assert called[1][0] == "search_listings"
    assert called[1][1]["price_max"] == 1_000_000
    assert called[1][1]["district"] == "Phường Bình Thạnh"


@pytest.mark.asyncio
async def test_missing_province_asks_for_location_instead_of_using_rag(monkeypatch):
    class Repo:
        async def get(self, user_id, conversation_id):
            return {"searchContext": None, "searchState": None, "messages": []}

        async def append_message(self, *args, **kwargs):
            return True

    class UseCases:
        async def ask(self, **kwargs):
            raise AssertionError("Listing search must not be sent to RAG")

    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.get_conversation_repository", Repo)
    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.get_generative_client", lambda settings: object())
    monkeypatch.setattr("homespace_ai.api.v1.agent_ask.AskUseCases", lambda **kwargs: UseCases())
    user = UserContext.model_validate({"X-User-Id": "test-user", "X-User-Role": "USER"})
    response = await ask_agent(
        AskRequest(question="Tìm phòng trọ dưới 3 triệu", conversationId="conversation-1"),
        user=user, session=object(), settings=SimpleNamespace(), embedder=object(),
    )
    assert response.result.status == "PROPERTY_SEARCH"
    assert "tỉnh/thành" in response.result.answer
