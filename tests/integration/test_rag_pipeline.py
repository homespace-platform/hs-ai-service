import uuid
import pytest
from httpx import ASGITransport, AsyncClient

from homespace_ai.core.config import get_settings
from homespace_ai.core.database import async_session_maker
from homespace_ai.knowledge.ingestion.embedder import MockEmbeddingAdapter
from homespace_ai.knowledge.ingestion.worker import IngestionWorker
from homespace_ai.main import app
from homespace_ai.models.knowledge import KnowledgeDocument, KnowledgeDocumentVersion
from homespace_ai.repositories.document_repo import DocumentRepository

def create_sample_md(doc_id: str, title: str, visibility: str, body: str) -> str:
    return f"""---
document_id: {doc_id}
title: {title}
audience: [tenant, landlord]
visibility: {visibility}
status: draft
version: 1.0.0
locale: vi-VN
category: FAQ
---

{body}
"""


@pytest.mark.asyncio
async def test_full_rag_lifecycle_integration():
    settings = get_settings()
    unique_suffix = uuid.uuid4().hex[:6]
    test_pub_id = f"hs-int-test-faq-{unique_suffix}"
    test_adm_id = f"hs-int-test-admin-{unique_suffix}"

    sample_public = create_sample_md(
        test_pub_id,
        f"Thí nghiệm thiên văn học hành tinh Xylo {unique_suffix}",
        "public",
        f"# Báo cáo khoa học thiên văn học Xylo {unique_suffix}\n\nHành tinh Xylo {unique_suffix} có đúng ba vệ tinh tự nhiên quay quanh quỹ đạo đồng bộ.\nCác vệ tinh này được phát hiện vào năm 2026 bởi nhóm nghiên cứu HomeSpace."
    )
    sample_admin = create_sample_md(
        test_adm_id,
        f"Tài liệu mật trạm quan trắc không gian {unique_suffix}",
        "admin",
        f"# Quy trình mật trạm quan trắc không gian {unique_suffix}\n\nTrạm quan trắc quỹ đạo mật {unique_suffix} chỉ phục vụ nhân sự quản trị viên tối cao.\nTài liệu này tuyệt đối không được tiết lộ cho bất kỳ ai ngoài ban quản trị."
    )
    admin_headers = {
        "X-User-Id": "admin-test-uuid",
        "X-User-Role": "ADMIN",
        "X-User-Authorities": "ROLE_ADMIN",
        "X-Internal-Secret": settings.gateway_internal_secret,
    }
    user_headers = {
        "X-User-Id": "user-test-uuid",
        "X-User-Role": "USER",
        "X-User-Authorities": "ROLE_USER",
        "X-Internal-Secret": settings.gateway_internal_secret,
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Upload public document as draft
        res_upload = await client.post(
            "/admin/knowledge/documents",
            files={"file": ("xylo-study.md", sample_public.encode("utf-8"), "text/markdown")},
            headers=admin_headers,
        )
        assert res_upload.status_code == 201
        data = res_upload.json()["result"]
        doc_id = data["documentId"]
        assert data["status"] == "DRAFT"

        # 2. Upload admin-only document as draft
        res_admin_upload = await client.post(
            "/admin/knowledge/documents",
            files={"file": ("xylo-admin-secret.md", sample_admin.encode("utf-8"), "text/markdown")},
            headers=admin_headers,
        )
        assert res_admin_upload.status_code == 201
        admin_doc_id = res_admin_upload.json()["result"]["documentId"]

        # 3. Before approval & publishing, user asking question gets NO_EVIDENCE
        test_question = f"Hành tinh Xylo {unique_suffix} có bao nhiêu vệ tinh tự nhiên quay quanh?"
        res_ask_before = await client.post(
            "/agent/ask",
            json={"question": test_question},
            headers=user_headers,
        )
        assert res_ask_before.status_code == 200
        assert res_ask_before.json()["result"]["status"] == "NO_EVIDENCE"

        # 4. Admin approves both documents
        res_app_1 = await client.post(f"/admin/knowledge/documents/{doc_id}/approve", headers=admin_headers)
        assert res_app_1.status_code == 200
        res_app_2 = await client.post(f"/admin/knowledge/documents/{admin_doc_id}/approve", headers=admin_headers)
        assert res_app_2.status_code == 200

        # 5. Admin publishes both documents (enqueues jobs)
        res_pub_1 = await client.post(f"/admin/knowledge/documents/{doc_id}/publish", headers=admin_headers)
        assert res_pub_1.status_code == 202
        job_id_1 = res_pub_1.json()["result"]["job_id"]

        res_pub_2 = await client.post(f"/admin/knowledge/documents/{admin_doc_id}/publish", headers=admin_headers)
        assert res_pub_2.status_code == 202
        job_id_2 = res_pub_2.json()["result"]["job_id"]

        # 6. Run Worker to process both jobs
        worker = IngestionWorker()
        processed_1 = await worker.process_next_job()
        assert processed_1 is True

        processed_2 = await worker.process_next_job()
        assert processed_2 is True

        # 7. Check job status is COMPLETED
        res_job = await client.get(f"/admin/knowledge/jobs/{job_id_1}", headers=admin_headers)
        assert res_job.status_code == 200
        assert res_job.json()["result"]["status"] == "COMPLETED"

        # 8. User asks question matching public FAQ
        res_ask = await client.post(
            "/agent/ask",
            json={"question": test_question},
            headers=user_headers,
        )
        assert res_ask.status_code == 200
        ask_data = res_ask.json()["result"]
        assert ask_data["status"] == "ANSWERED"
        assert len(ask_data["citations"]) > 0
        assert ask_data["citations"][0]["documentId"] == doc_id

        # A new admin-only version must not change the active public ACL until
        # activation; after activation it must be excluded for normal users.
        restricted_version = create_sample_md(
            doc_id,
            "Xylo restricted policy",
            "admin",
            f"# Xylo restricted\n\nHành tinh Xylo {unique_suffix} có ba vệ tinh và là tài liệu nội bộ.",
        ).replace("version: 1.0.0", "version: 2.0.0")
        uploaded_restricted = await client.post(
            "/admin/knowledge/documents",
            files={"file": ("xylo-restricted.md", restricted_version.encode("utf-8"), "text/markdown")},
            headers=admin_headers,
        )
        assert uploaded_restricted.status_code == 201
        before_activation = await client.post(
            "/agent/ask", json={"question": test_question}, headers=user_headers
        )
        assert before_activation.json()["result"]["status"] == "ANSWERED"
        assert (await client.post(
            f"/admin/knowledge/documents/{doc_id}/approve", headers=admin_headers
        )).status_code == 200
        assert (await client.post(
            f"/admin/knowledge/documents/{doc_id}/publish", headers=admin_headers
        )).status_code == 202
        assert await worker.process_next_job() is True
        after_activation = await client.post(
            "/agent/ask", json={"question": test_question}, headers=user_headers
        )
        assert all(
            citation["documentId"] != doc_id
            for citation in after_activation.json()["result"]["citations"]
        )

        # 9. Real-time query test: user asks personal account query -> OUT_OF_SCOPE
        res_realtime = await client.post(
            "/agent/ask",
            json={"question": "Tiền thuê tháng này của tôi đã thanh toán chưa?"},
            headers=user_headers,
        )
        assert res_realtime.status_code == 200
        assert res_realtime.json()["result"]["status"] == "OUT_OF_SCOPE"
        assert len(res_realtime.json()["result"]["citations"]) == 0

        # 10. Security ACL test: User asks verbatim about admin-only document -> NEVER exposed!
        res_admin_leak_attempt = await client.post(
            "/agent/ask",
            json={"question": f"Quy trình mật trạm quan trắc không gian {unique_suffix} là gì?"},
            headers=user_headers,
        )
        assert res_admin_leak_attempt.status_code == 200
        user_attempt_data = res_admin_leak_attempt.json()["result"]
        # Must NOT return admin document citation
        for cit in user_attempt_data["citations"]:
            assert cit["documentId"] != admin_doc_id

        # 11. Admin archiving document: Immediately excluded from retrieval
        res_archive = await client.post(f"/admin/knowledge/documents/{doc_id}/archive", headers=admin_headers)
        assert res_archive.status_code == 200

        res_ask_after_archive = await client.post(
            "/agent/ask",
            json={"question": test_question},
            headers=user_headers,
        )
        assert res_ask_after_archive.status_code == 200
        assert res_ask_after_archive.json()["result"]["status"] == "NO_EVIDENCE"
