from types import SimpleNamespace

import pytest

from homespace_ai.application.use_cases.ask_use_cases import AskUseCases
from homespace_ai.clients.generative import generation_prompts


class FakeGenerator:
    def __init__(self):
        self.calls = []

    async def generate_answer(self, question, context_chunks, *, audience="USER", mode="homespace"):
        self.calls.append((question, context_chunks, audience, mode))
        return "Mình có thể giải thích kiến thức chung, nhưng thông tin hiện tại cần kiểm chứng."


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed_query(self, question):
        self.calls.append(question)
        return [0.1]


class FakeRepository:
    def __init__(self):
        self.visibilities = None

    async def search_chunks_hybrid(self, **kwargs):
        self.visibilities = kwargs["allowed_visibilities"]
        return []


def build_use_case():
    generator = FakeGenerator()
    embedder = FakeEmbedder()
    case = AskUseCases(
        session=None,
        settings=SimpleNamespace(retrieval_top_k=5, retrieval_min_similarity=0.8),
        embedder=embedder,
        generative_client=generator,
    )
    case.chunk_repo = FakeRepository()
    return case, generator, embedder


@pytest.mark.asyncio
async def test_greeting_uses_trusted_customer_name_without_retrieval():
    case, generator, embedder = build_use_case()

    result = await case.ask("Chào bạn", user_role="USER", user_name="Nguyễn Văn An")

    assert result.answer.startswith("Chào Nguyễn Văn An!")
    assert "chủ nhà" not in result.answer
    assert result.status == "ANSWERED"
    assert not generator.calls and not embedder.calls


@pytest.mark.asyncio
async def test_admin_general_topic_does_not_use_homespace_rag():
    case, generator, embedder = build_use_case()

    result = await case.ask("Bạn biết chủ tịch nước Việt Nam là ai không?", user_role="ADMIN")

    assert result.status == "GENERAL_ANSWER"
    assert result.citations == []
    assert generator.calls[0][2:] == ("ADMIN", "general")
    assert not embedder.calls


@pytest.mark.asyncio
async def test_user_cannot_use_general_answer_or_admin_documents():
    case, generator, _ = build_use_case()

    result = await case.ask("Bạn biết chủ tịch nước Việt Nam là ai không?", user_role="USER")

    assert result.status == "NO_EVIDENCE"
    assert case.chunk_repo.visibilities == ["public"]
    assert not generator.calls


@pytest.mark.asyncio
async def test_admin_homespace_query_can_search_admin_documents():
    case, generator, _ = build_use_case()

    result = await case.ask("HomeSpace kiểm duyệt tin đăng thế nào?", user_role="ADMIN")

    assert case.chunk_repo.visibilities == ["public", "admin"]
    assert result.status == "NO_EVIDENCE"
    assert not generator.calls


def test_role_prompts_do_not_mix_client_and_admin_personas():
    user_prompt, _ = generation_prompts("Hợp đồng?", [], "USER", "homespace")
    admin_prompt, _ = generation_prompts("Hợp đồng?", [], "ADMIN", "homespace")
    general_prompt, question = generation_prompts("Lịch sử Việt Nam?", [], "ADMIN", "general")

    assert "có thể vừa cho thuê nhà vừa đi thuê" in user_prompt
    assert "ADMIN đã xác thực" in admin_prompt
    assert "KHÔNG lấy từ tài liệu HomeSpace" in general_prompt
    assert question == "Lịch sử Việt Nam?"
