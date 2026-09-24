import os

import pytest
from sqlalchemy.engine import make_url

from homespace_ai.core.config import get_settings


@pytest.fixture(autouse=True)
def require_dedicated_test_database():
    database_name = make_url(get_settings().ai_database_url).database or ""
    if not database_name.endswith("_test"):
        pytest.skip("Integration tests require a dedicated database ending in _test.")
    if os.getenv("HS_RUN_RAG_INTEGRATION") != "1":
        pytest.skip("Set HS_RUN_RAG_INTEGRATION=1 to run database integration tests.")


@pytest.fixture(autouse=True)
def fake_generation(monkeypatch):
    class FakeClient:
        async def generate_answer(self, question, context_chunks):
            return context_chunks[0]["content"] if context_chunks else ""

    monkeypatch.setattr(
        "homespace_ai.api.v1.agent_ask.get_generative_client",
        lambda settings: FakeClient(),
    )
