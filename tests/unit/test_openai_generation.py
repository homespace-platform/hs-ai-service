import json

import httpx
import pytest
from pydantic import ValidationError

import homespace_ai.clients.generative as module
from homespace_ai.clients.generative import (
    DisabledGenerativeClient, GenerationUnavailableError, OpenAIGenerativeClient,
    OpenAIResponsesClient, get_generative_client,
)
from homespace_ai.core.config import Settings


def settings(**kwargs):
    return Settings(_env_file=None, GENERATION_PROVIDER="openai", OPENAI_API_KEY="test-only-key", **kwargs)


def completed(text="Đây là câu trả lời trọn vẹn."):
    return {"status": "completed", "output": [
        {"type": "reasoning", "summary": [{"text": "not public"}]},
        {"type": "message", "content": [{"type": "output_text", "text": text}]},
    ]}


def mock_http(monkeypatch, replies):
    calls = []
    real_client = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw:
                        real_client(transport=httpx.MockTransport(handler), **kw))

    async def no_wait(delay):
        pass
    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,model,budget", [
    ("homespace", "gpt-4o-mini", 4096),
    ("general", "gpt-4o-mini", 4096),
    ("listing", "gpt-4.1-mini", 16384),
])
async def test_task_model_routing_and_responses_payload(monkeypatch, mode, model, budget):
    calls = mock_http(monkeypatch, [httpx.Response(200, json=completed())])
    answer = await get_generative_client(settings()).generate_answer(
        "Câu hỏi?", [{"content": "{}"}], audience="ADMIN", mode=mode)
    payload = json.loads(calls[0].content)
    assert str(calls[0].url) == "https://api.openai.com/v1/responses"
    assert calls[0].headers["authorization"] == "Bearer test-only-key"
    assert payload["model"] == model
    assert payload["max_output_tokens"] == budget
    assert payload["store"] is False
    assert "reasoning" not in payload
    assert payload["temperature"] == 0.2
    assert answer == "Đây là câu trả lời trọn vẹn."


@pytest.mark.asyncio
async def test_large_core_evidence_is_not_compacted(monkeypatch):
    evidence = json.dumps({"listings": [{"all_fields": "nội thất " * 30000, "last_field": "KEPT"}]},
                          ensure_ascii=False)
    client = OpenAIGenerativeClient(settings())
    seen = {}
    async def fake_create(payload, *, task):
        seen.update(payload)
        assert task == "listing_answer"
        return completed()
    monkeypatch.setattr(client.transport, "create", fake_create)
    await client.generate_answer("So sánh", [{"content": evidence}], mode="listing")
    assert seen["input"][0]["content"].endswith(evidence)


@pytest.mark.asyncio
async def test_per_task_model_overrides(monkeypatch):
    client = OpenAIGenerativeClient(settings(
        OPENAI_RAG_MODEL="gpt-4.1", OPENAI_LISTING_MODEL="gpt-5.4",
        OPENAI_REASONING_EFFORT="medium"))
    seen = []
    async def fake_create(payload, *, task):
        seen.append(payload)
        return completed()
    monkeypatch.setattr(client.transport, "create", fake_create)
    await client.generate_answer("?", [])
    await client.generate_answer("?", [], mode="listing")
    assert [item["model"] for item in seen] == ["gpt-4.1", "gpt-5.4"]
    assert seen[1]["reasoning"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_partial_answer_is_retried_not_returned(monkeypatch):
    partial = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
               "output": [{"type": "message", "content": [{"type": "output_text", "text": "Dở dang"}]}]}
    calls = mock_http(monkeypatch, [httpx.Response(200, json=partial), httpx.Response(200, json=completed())])
    answer = await get_generative_client(settings()).generate_answer("So sánh", [], mode="listing")
    assert answer != "Dở dang"
    assert [json.loads(call.content)["max_output_tokens"] for call in calls] == [16384, 32768]


@pytest.mark.asyncio
async def test_repeated_truncation_fails_safely(monkeypatch):
    calls = mock_http(monkeypatch, [httpx.Response(200, json={
        "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []})])
    with pytest.raises(GenerationUnavailableError, match="output-token"):
        await get_generative_client(settings()).generate_answer("So sánh", [], mode="listing")
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,count", [
    (429, "rate_limit_exceeded", 2), (503, "server_error", 2),
    (429, "insufficient_quota", 1), (401, "invalid_api_key", 1), (400, "invalid_request", 1),
])
async def test_bounded_retries_and_safe_error_messages(monkeypatch, status, code, count):
    calls = mock_http(monkeypatch, [httpx.Response(status, json={
        "error": {"code": code, "message": "secret prompt and test-only-key"}})])
    with pytest.raises(GenerationUnavailableError) as error:
        await get_generative_client(settings()).generate_answer("secret prompt", [])
    assert "secret" not in str(error.value)
    assert len(calls) == count


@pytest.mark.asyncio
async def test_network_retry(monkeypatch):
    calls = mock_http(monkeypatch, [
        httpx.ConnectError("connection failed"), httpx.Response(200, json=completed())])
    assert await get_generative_client(settings()).generate_answer("?", [])
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    {"status": "failed", "output": []},
    {"status": "completed", "output": []},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]},
    {"output": "invalid"},
    {"status": "completed", "output": [None]},
    {"status": "completed", "output": [{"type": "message", "content": None}]},
    {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": 123}]}]},
])
async def test_no_empty_or_failed_answers(monkeypatch, data):
    mock_http(monkeypatch, [httpx.Response(200, json=data)])
    with pytest.raises(GenerationUnavailableError):
        await get_generative_client(settings()).generate_answer("?", [])


@pytest.mark.asyncio
async def test_key_is_required_and_redacted():
    config = Settings(_env_file=None, GENERATION_PROVIDER="openai", OPENAI_API_KEY="")
    with pytest.raises(GenerationUnavailableError, match="not configured"):
        await get_generative_client(config).generate_answer("?", [])
    secret = settings()
    assert "test-only-key" not in repr(secret)
    assert "test-only-key" not in secret.model_dump_json()


def test_disabled_and_local_embedding_unchanged():
    config = Settings(_env_file=None, GENERATION_PROVIDER="disabled")
    assert isinstance(get_generative_client(config), DisabledGenerativeClient)
    assert config.embedding_model_id == "intfloat/multilingual-e5-small"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, GENERATION_PROVIDER="groq")


@pytest.mark.asyncio
async def test_logs_contain_usage_not_prompt_or_key(monkeypatch):
    events = []
    class Logger:
        def info(self, event, **values):
            events.append((event, values))
        def warning(self, event, **values):
            events.append((event, values))
    monkeypatch.setattr(module, "logger", Logger())
    result = completed()
    result["usage"] = {"input_tokens": 100, "output_tokens": 20,
                       "input_tokens_details": {"cached_tokens": 10},
                       "output_tokens_details": {"reasoning_tokens": 5}}
    mock_http(monkeypatch, [httpx.Response(200, json=result)])
    await get_generative_client(settings()).generate_answer("private customer question", [])
    assert events[0][1]["input_tokens"] == 100
    assert events[0][1]["cached_tokens"] == 10
    assert "private customer" not in str(events)
    assert "test-only-key" not in str(events)
