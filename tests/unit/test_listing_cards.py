import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from homespace_ai.api.v1.agent_conversations import detail
from homespace_ai.application.listing_context import context_for
from homespace_ai.clients.generative import ListingGeneratedAnswer, GenerationUnavailableError, OpenAIGenerativeClient
from homespace_ai.repositories.conversation_repo import ConversationRepository
from tests.unit.test_openai_generation import settings, completed, mock_http
from tests.unit.test_listing_followup import IDS, CurrentTools, previous
from tests.unit.test_listing_ask import make_assistant


@pytest.mark.asyncio
async def test_structured_answer_schema_constrains_ids_and_keeps_full_evidence(monkeypatch):
    evidence = {"listings": [{"id": value, "last_field": "KEPT"} for value in IDS[:4]]}
    calls = mock_http(monkeypatch, [httpx.Response(200, json=completed(json.dumps({
        "answer": "Mình đề xuất Phòng 3 vì giá thuê thấp nhất trong nhóm.", "listingIds": [IDS[2]]})))])
    result = await OpenAIGenerativeClient(settings()).generate_listing_answer("Chọn phòng rẻ nhất", evidence)
    payload = json.loads(calls[0].content)
    schema = payload["text"]["format"]
    assert schema["strict"] is True
    assert schema["schema"]["properties"]["listingIds"]["items"]["enum"] == IDS[:4]
    assert "KEPT" in payload["input"][0]["content"]
    assert result.listing_ids == [IDS[2]]
    assert "Phòng 3" in result.answer


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["not json", json.dumps({"answer": "OK", "listingIds": [IDS[4]]}),
    json.dumps({"answer": "", "listingIds": []}), json.dumps({"answer": "OK", "listingIds": "bad"})])
async def test_invalid_model_output_never_becomes_a_card(monkeypatch, raw):
    mock_http(monkeypatch, [httpx.Response(200, json=completed(raw))])
    with pytest.raises(GenerationUnavailableError):
        await OpenAIGenerativeClient(settings()).generate_listing_answer("?", {"listings": [{"id": IDS[0]}]})


@pytest.mark.asyncio
async def test_recommendation_cards_are_separate_from_followup_scope_and_remove_markdown_links():
    assistant = make_assistant(None)
    assistant.tools = CurrentTools()
    assistant.generative_client.generate_listing_answer = AsyncMock(return_value=ListingGeneratedAnswer(
        f"Mình chọn [Phòng 3](/rent/{IDS[2]}) cho bạn.", [IDS[2], IDS[2]]))
    result = await assistant.try_answer("Nên chọn phòng nào rẻ nhất?", previous(), "cards")
    assert result.listing_ids == [IDS[2]]
    assert result.listing_positions == {IDS[2]: 3}
    assert result.listing_context == context_for(IDS[:4])
    assert "/rent/" not in result.answer


@pytest.mark.asyncio
async def test_backend_rejects_unverified_id_even_if_generator_returns_it():
    assistant = make_assistant(None)
    assistant.tools = CurrentTools()
    assistant.generative_client.generate_listing_answer = AsyncMock(return_value=ListingGeneratedAnswer("OK", [IDS[4]]))
    result = await assistant.try_answer("Nên chọn phòng nào?", previous(), "unknown")
    assert result.status == "GENERATION_UNAVAILABLE"
    assert result.listing_ids == []


@pytest.mark.asyncio
async def test_card_ids_and_ordinals_survive_owner_scoped_history_roundtrip():
    repo = ConversationRepository.__new__(ConversationRepository)
    repo.collection = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)))
    await repo.append_message("owner", "conv", role="assistant", content="Phòng 3", status="ANSWERED",
        listing_context=context_for(IDS[:4]), listing_ids=[IDS[2]], listing_positions={IDS[2]: 3})
    call = repo.collection.update_one.call_args
    assert call.args[0] == {"_id": "conv", "ownerId": "owner"}
    message = call.args[1]["$push"]["messages"]
    document = {"_id": "conv", "title": "Chat", "isPinned": False,
        "createdAt": message["createdAt"], "updatedAt": message["createdAt"], "messages": [message]}
    public = detail(document).model_dump()
    assert public["messages"][0]["listingIds"] == [IDS[2]]
    assert public["messages"][0]["listingPositions"] == {IDS[2]: 3}
    assert "listingContext" not in public["messages"][0]
    document["messages"] = [{k: v for k, v in message.items() if k not in {"listingIds", "listingPositions"}}]
    assert detail(document).messages[0].listingIds == []  # Old histories require no migration.
