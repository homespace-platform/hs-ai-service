"""Database-backed lifecycle tests. Only a dedicated *_test database is allowed by conftest.

No OpenAI calls or embedding weight downloads: use a deterministic local test adapter.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from homespace_ai.application.use_cases.document_use_cases import DocumentUseCases
from homespace_ai.core.config import get_settings
from homespace_ai.core.database import async_session_maker
from homespace_ai.knowledge.ingestion.worker import IngestionWorker
from homespace_ai.main import app
from homespace_ai.models.knowledge import KnowledgeChunk, KnowledgeDocumentVersion
from homespace_ai.repositories.document_repo import DocumentRepository
from homespace_ai.repositories.job_repo import JobRepository


class TestTokenizer:
    __test__ = False

    def encode(self, text, add_special_tokens=False):
        return text.split()

    def decode(self, tokens, **kwargs):
        return " ".join(tokens)


class TestEmbedding:
    __test__ = False
    tokenizer = TestTokenizer()

    def embed_passages(self, passages):
        return [[1.0] + [0.0] * 383 for _ in passages]


def markdown(body="Nội dung hướng dẫn thuê nhà", visibility="public", document_id="front-matter-id", version="999"):
    return f"---\ndocument_id: {document_id}\ntitle: Hướng dẫn\nvisibility: {visibility}\naudience: [tenant]\nversion: {version}\nlocale: vi-VN\n---\n\n# Hướng dẫn\n\n{body}\n"


async def upload(filename, content=None):
    async with async_session_maker() as session:
        return await DocumentUseCases(session, get_settings()).upload_document(
            filename, (content or markdown()).encode("utf-8"), "admin-test")


async def process_version(result, worker_id="managed-test-worker"):
    # Claim this exact job so other integration fixtures cannot affect ordering.
    from datetime import datetime, timedelta, timezone
    async with async_session_maker() as session:
        job = await JobRepository(session).get_by_id(uuid.UUID(result.job_id))
        job.status = "RUNNING"
        job.worker_id = worker_id
        job.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        await session.commit()
    worker = IngestionWorker(embedder=TestEmbedding(), worker_id=worker_id)
    await worker._execute_ingestion(uuid.UUID(result.job_id), uuid.UUID(result.version_id), result.document_id)


@pytest.mark.asyncio
async def test_same_filename_and_identical_content_always_increment_and_replace_vectors():
    name = f"version-{uuid.uuid4().hex}.md"
    first = await upload(name)
    assert first.version == "1" and first.status == "QUEUED"
    await process_version(first)
    second = await upload(name.upper())
    assert second.version == "2" and second.document_id == first.document_id
    async with async_session_maker() as session:
        doc = await DocumentRepository(session).get_by_document_id(first.document_id)
        assert str(doc.active_version_id) == first.version_id  # old facts survive while queued
    await process_version(second)
    async with async_session_maker() as session:
        doc = await DocumentRepository(session).get_by_document_id(first.document_id)
        assert str(doc.active_version_id) == second.version_id
        assert len(doc.versions) == 2  # keep audit history, not obsolete vectors
        assert doc.versions[1].status == "ARCHIVED"
        ids = (await session.execute(select(KnowledgeChunk.version_id).where(KnowledgeChunk.document_id == first.document_id))).scalars().all()
        assert ids and set(ids) == {uuid.UUID(second.version_id)}
    third = await upload(f"different-{uuid.uuid4().hex}.md")
    assert third.document_id != first.document_id  # same front-matter ID does not merge different files


@pytest.mark.asyncio
async def test_editor_update_has_optimistic_lock_and_keeps_acl_until_activation():
    first = await upload(f"editor-{uuid.uuid4().hex}.md")
    await process_version(first)
    async with async_session_maker() as session:
        use_cases = DocumentUseCases(session, get_settings())
        content = await use_cases.get_content(first.document_id)
        assert content["markdown"].startswith("---\n") and "version: '1'" in content["markdown"]
        second = await use_cases.update_content(first.document_id, markdown("Nội dung nội bộ mới", "admin"), first.version_id)
    async with async_session_maker() as session:
        doc = await DocumentRepository(session).get_by_document_id(first.document_id)
        assert doc.visibility == "public"
        with pytest.raises(HTTPException) as conflict:
            await DocumentUseCases(session, get_settings()).update_content(first.document_id, markdown(), first.version_id)
        assert conflict.value.status_code == 409
        await session.rollback()
    await process_version(second)
    async with async_session_maker() as session:
        doc = await DocumentRepository(session).get_by_document_id(first.document_id)
        assert doc.visibility == "admin" and str(doc.active_version_id) == second.version_id


@pytest.mark.asyncio
async def test_older_worker_cannot_overwrite_newer_activated_version():
    filename = f"ordering-{uuid.uuid4().hex}.md"
    first = await upload(filename)
    second = await upload(filename, markdown("Bản mới hơn"))
    await process_version(second)
    await process_version(first)
    async with async_session_maker() as session:
        doc = await DocumentRepository(session).get_by_document_id(first.document_id)
        assert str(doc.active_version_id) == second.version_id
        old_chunks = (await session.execute(select(func.count()).select_from(KnowledgeChunk)
                       .where(KnowledgeChunk.version_id == uuid.UUID(first.version_id)))).scalar_one()
        assert old_chunks == 0


@pytest.mark.asyncio
async def test_failed_new_ingestion_preserves_old_active_vectors():
    filename = f"failure-{uuid.uuid4().hex}.md"
    first = await upload(filename)
    await process_version(first)
    second = await upload(filename)
    class BrokenEmbedding(TestEmbedding):
        def embed_passages(self, passages):
            raise RuntimeError("Local embedding failure")
    async with async_session_maker() as session:
        job = await JobRepository(session).get_by_id(uuid.UUID(second.job_id))
        from datetime import datetime, timedelta, timezone
        job.status = "RUNNING"
        job.worker_id = "failed-test-worker"
        job.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        await session.commit()
    worker = IngestionWorker(embedder=BrokenEmbedding(), worker_id="failed-test-worker")
    with pytest.raises(RuntimeError):
        await worker._execute_ingestion(uuid.UUID(second.job_id), uuid.UUID(second.version_id), second.document_id)
    async with async_session_maker() as session:
        doc = await DocumentRepository(session).get_by_document_id(first.document_id)
        assert str(doc.active_version_id) == first.version_id
        count = (await session.execute(select(func.count()).select_from(KnowledgeChunk)
                  .where(KnowledgeChunk.version_id == uuid.UUID(first.version_id)))).scalar_one()
        assert count > 0


@pytest.mark.asyncio
async def test_parallel_uploads_allocate_distinct_sequential_versions():
    filename = f"parallel-{uuid.uuid4().hex}.md"
    results = await asyncio.gather(*(upload(filename) for _ in range(4)))
    assert sorted(int(result.version) for result in results) == [1, 2, 3, 4]
    assert len({result.document_id for result in results}) == 1


@pytest.mark.asyncio
async def test_batch_partial_errors_and_admin_content_contract():
    settings = get_settings()
    headers = {"X-User-Id": "admin-test", "X-User-Role": "ADMIN", "X-Internal-Secret": settings.gateway_internal_secret}
    files = [("files", (f"batch-{uuid.uuid4().hex}.md", markdown().encode(), "text/markdown")),
             ("files", ("invalid.txt", b"not markdown", "text/plain")),
             ("files", ("README.md", b"folder guide", "text/markdown"))]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/knowledge/documents/batch", files=files, headers=headers)
        assert response.status_code == 202
        items = response.json()["result"]["items"]
        assert [item["success"] for item in items] == [True, False, False]
        first = items[0]["result"]
        editor = await client.get(f"/admin/knowledge/documents/{first['documentId']}/content", headers=headers)
        assert editor.status_code == 200
        updated = await client.put(f"/admin/knowledge/documents/{first['documentId']}/content", headers=headers,
            json={"markdown": editor.json()["result"]["markdown"], "expectedVersionId": first["versionId"]})
        assert updated.status_code == 202 and updated.json()["result"]["version"] == "2"
        denied = await client.get(f"/admin/knowledge/documents/{first['documentId']}/content",
            headers={**headers, "X-User-Role": "USER"})
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_migrated_documents_keep_identity_and_receive_next_version():
    async with async_session_maker() as session:
        doc = await DocumentRepository(session).get_by_document_id("legacy-test-0")
        if not doc:
            pytest.skip("Legacy migration fixtures are created by scripts/test_managed_knowledge.py.")
        assert doc.source_filename == "legacy-unique.md"
        assert [version.version_number for version in doc.versions] == [2, 1]
        assert [version.version for version in doc.versions] == ["2", "1"]
    result = await upload("Legacy-Unique.MD")
    assert result.document_id == "legacy-test-0" and result.version == "3"


@pytest.mark.asyncio
async def test_ambiguous_legacy_filenames_are_not_silently_merged():
    async with async_session_maker() as session:
        if not await DocumentRepository(session).get_by_document_id("legacy-test-1"):
            pytest.skip("Legacy migration fixtures are created by scripts/test_managed_knowledge.py.")
    with pytest.raises(HTTPException) as conflict:
        await upload("legacy-duplicate.md")
    assert conflict.value.status_code == 409
