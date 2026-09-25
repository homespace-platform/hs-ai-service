import pytest

import homespace_ai.clients.generative as generative_module
from homespace_ai.clients.generative import (
    BaseGenerativeClient,
    FallbackGenerativeClient,
    GeminiGenerativeClient,
    GenerationUnavailableError,
    GroqGenerativeClient,
    get_generative_client,
)
from homespace_ai.core.config import Settings


class StubGenerativeClient(BaseGenerativeClient):
    def __init__(
        self,
        *,
        answer: str = "",
        error: GenerationUnavailableError | None = None,
    ) -> None:
        self.answer = answer
        self.error = error
        self.calls = 0

    async def generate_answer(self, question, context_chunks):
        self.calls += 1
        if self.error:
            raise self.error
        return self.answer


@pytest.mark.asyncio
async def test_fallback_uses_second_provider_for_retryable_error():
    primary = StubGenerativeClient(
        error=GenerationUnavailableError("rate limited")
    )
    fallback = StubGenerativeClient(answer="fallback answer")
    client = FallbackGenerativeClient(
        [("gemini", primary), ("groq", fallback)]
    )

    answer = await client.generate_answer("question", [])

    assert answer == "fallback answer"
    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_fallback_does_not_hide_non_retryable_error():
    primary = StubGenerativeClient(
        error=GenerationUnavailableError("invalid request", retryable=False)
    )
    fallback = StubGenerativeClient(answer="must not be used")
    client = FallbackGenerativeClient(
        [("gemini", primary), ("groq", fallback)]
    )

    with pytest.raises(GenerationUnavailableError, match="invalid request"):
        await client.generate_answer("question", [])

    assert primary.calls == 1
    assert fallback.calls == 0


def test_factory_uses_provider_specific_models():
    settings = Settings(
        _env_file=None,
        GENERATION_PROVIDER="gemini",
        GENERATION_FALLBACK_PROVIDER="groq",
        GENERATION_MODEL="legacy-primary-model",
        GEMINI_MODEL="gemini-test-model",
        GROQ_MODEL="groq-test-model",
    )

    client = get_generative_client(settings)

    assert isinstance(client, FallbackGenerativeClient)
    assert isinstance(client.clients[0][1], GeminiGenerativeClient)
    assert client.clients[0][1].model == "gemini-test-model"
    assert isinstance(client.clients[1][1], GroqGenerativeClient)
    assert client.clients[1][1].model == "groq-test-model"


@pytest.mark.asyncio
async def test_groq_gpt_oss_uses_low_reasoning_budget(monkeypatch):
    captured_payload = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "answer"}}]}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, json, headers):
            captured_payload.update(json)
            return FakeResponse()

    monkeypatch.setattr(generative_module.httpx, "AsyncClient", FakeAsyncClient)
    settings = Settings(
        _env_file=None,
        GENERATION_PROVIDER="groq",
        GROQ_MODEL="openai/gpt-oss-20b",
        GROQ_API_KEY="test-key",
    )

    answer = await GroqGenerativeClient(settings).generate_answer("question", [])

    assert answer == "answer"
    assert captured_payload["reasoning_effort"] == "low"
    assert captured_payload["include_reasoning"] is False
    assert captured_payload["max_completion_tokens"] == 1024
    assert "max_tokens" not in captured_payload
