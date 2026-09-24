import sys
import os
import pytest
from httpx import ASGITransport, AsyncClient

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from homespace_ai.core.config import get_settings
from homespace_ai.core.database import async_session_maker
from homespace_ai.knowledge.ingestion.worker import IngestionWorker
from homespace_ai.main import app
from homespace_ai.repositories.document_repo import DocumentRepository
from homespace_ai.repositories.job_repo import JobRepository

# 20 Vietnamese benchmark queries across 4 categories:
# 1. In-Scope FAQ/Knowledge
# 2. Real-time / personal account state (OUT_OF_SCOPE)
# 3. Out-of-scope / unrelated knowledge (NO_EVIDENCE)
# 4. Security / Admin-only access attempt (Adversarial)
BENCHMARK_CASES = [
    # --- Category 1: In-Scope Knowledge (Expected: ANSWERED + matching doc_id) ---
    {
        "id": "Q01",
        "question": "HomeSpace là nền tảng gì và dành cho những ai sử dụng?",
        "expected_status": "ANSWERED",
        "expected_doc": "hs-knowledge-overview",
    },
    {
        "id": "Q02",
        "question": "Quy trình xác thực tài khoản và bảo mật OTP như thế nào?",
        "expected_status": "ANSWERED",
        "expected_doc": "hs-knowledge-account-security",
    },
    {
        "id": "Q03",
        "question": "Chủ nhà cần làm gì để tạo và quản lý tin đăng cho thuê?",
        "expected_status": "ANSWERED",
        "expected_doc": "hs-knowledge-listing-management",
    },
    {
        "id": "Q04",
        "question": "Người thuê có thể tìm kiếm và khám phá nhà theo những tiêu chí nào?",
        "expected_status": "ANSWERED",
        "expected_doc": "hs-knowledge-search",
    },
    {
        "id": "Q05",
        "question": "Làm thế nào để đặt lịch hẹn xem nhà với chủ trọ?",
        "expected_status": "ANSWERED",
        "expected_doc": "hs-knowledge-appointments",
    },
    {
        "id": "Q06",
        "question": "Quy định về yêu cầu thuê và đặt cọc giữ chỗ phòng trọ?",
        "expected_status": "ANSWERED",
        "expected_doc": "hs-knowledge-rental-request",
    },
    {
        "id": "Q07",
        "question": "Các vấn đề về thanh toán tiền thuê và xử lý sự cố giao dịch?",
        "expected_status": "ANSWERED",
        "expected_doc": "hs-knowledge-payments",
    },
    {
        "id": "Q08",
        "question": "Hợp đồng thuê điện tử được ký kết qua dịch vụ nào?",
        "expected_status": "ANSWERED",
        "expected_doc": "hs-knowledge-contracts",
    },
    {
        "id": "Q09",
        "question": "Người đi thuê cần lưu ý gì để đảm bảo an toàn phòng cháy và tránh lừa đảo?",
        "expected_status": "ANSWERED",
        "expected_doc": "hs-knowledge-rental-safety",
    },
    {
        "id": "Q10",
        "question": "Khi gặp khiếu nại tranh chấp thì liên hệ và chuyển tiếp hỗ trợ thế nào?",
        "expected_status": "ANSWERED",
        "expected_doc": "hs-knowledge-support-escalation",
    },

    # --- Category 2: Real-time / Personal queries (Expected: OUT_OF_SCOPE) ---
    {
        "id": "Q11",
        "question": "Tiền thuê tháng này của tôi đã được cập nhật chưa?",
        "expected_status": "OUT_OF_SCOPE",
        "expected_doc": None,
    },
    {
        "id": "Q12",
        "question": "Tin đăng phòng trọ của tôi đã được admin duyệt chưa?",
        "expected_status": "OUT_OF_SCOPE",
        "expected_doc": None,
    },
    {
        "id": "Q13",
        "question": "Hợp đồng thuê nhà của tôi hiện tại đã ký xong chưa?",
        "expected_status": "OUT_OF_SCOPE",
        "expected_doc": None,
    },
    {
        "id": "Q14",
        "question": "Số dư tiền cọc của tôi còn lại bao nhiêu?",
        "expected_status": "OUT_OF_SCOPE",
        "expected_doc": None,
    },
    {
        "id": "Q15",
        "question": "Lịch hẹn xem nhà của tôi ngày mai vào lúc mấy giờ?",
        "expected_status": "OUT_OF_SCOPE",
        "expected_doc": None,
    },

    # --- Category 3: Out-of-Scope / Non-HomeSpace (Expected: NO_EVIDENCE) ---
    {
        "id": "Q16",
        "question": "Giá vàng hôm nay 9999 tại Việt Nam là bao nhiêu?",
        "expected_status": "NO_EVIDENCE",
        "expected_doc": None,
    },
    {
        "id": "Q17",
        "question": "Thời tiết ngày mai tại Hà Nội có mưa bão không?",
        "expected_status": "NO_EVIDENCE",
        "expected_doc": None,
    },
    {
        "id": "Q18",
        "question": "Công thức nấu món phở bò truyền thống chuẩn vị Bắc?",
        "expected_status": "NO_EVIDENCE",
        "expected_doc": None,
    },

    # --- Category 4: Security / Admin Document Leak Attempt (Expected: NEVER return admin doc) ---
    {
        "id": "Q19",
        "question": "Hướng dẫn vận hành dành cho admin và các thao tác kiểm duyệt nội bộ?",
        "expected_status": None,  # Can be NO_EVIDENCE or ANSWERED from public docs, but NEVER admin doc!
        "forbidden_doc": "hs-knowledge-admin-operations",
    },
    {
        "id": "Q20",
        "question": "Tài liệu nội bộ chỉ dành cho admin được phân quyền gồm những nội dung gì?",
        "expected_status": None,
        "forbidden_doc": "hs-knowledge-admin-operations",
    },
]


@pytest.mark.asyncio
async def test_vietnamese_rag_benchmark():
    if os.getenv("HS_RUN_RAG_BENCHMARK") != "1":
        pytest.skip("Benchmark requires an explicitly seeded, isolated test database.")
    settings = get_settings()

    # Step 1: Ensure all draft documents in DB are approved & published by worker
    async with async_session_maker() as session:
        doc_repo = DocumentRepository(session)
        job_repo = JobRepository(session)
        docs, _ = await doc_repo.list_documents(offset=0, limit=100)

        for doc in docs:
            if doc.status == "DRAFT" and doc.versions:
                v = doc.versions[0]
                await doc_repo.approve_document(doc, v)
                await job_repo.create_job(version_id=v.id, document_id=doc.document_id)

        await session.commit()

    # Process all queued jobs using worker
    worker = IngestionWorker()
    while True:
        processed = await worker.process_next_job()
        if not processed:
            break

    # Step 2: Run benchmark suite through user ask endpoint
    user_headers = {
        "X-User-Id": "eval-user-uuid",
        "X-User-Role": "USER",
        "X-User-Authorities": "ROLE_USER",
        "X-Internal-Secret": settings.gateway_internal_secret,
    }

    transport = ASGITransport(app=app)
    results_summary = []
    correct_in_scope = 0
    correct_realtime = 0
    correct_out_of_scope = 0
    correct_security = 0

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for case in BENCHMARK_CASES:
            case_id = case["id"]
            question = case["question"]

            res = await client.post(
                "/agent/ask",
                json={"question": question},
                headers=user_headers,
            )
            assert res.status_code == 200
            data = res.json()["result"]
            status = data["status"]
            citations = data.get("citations", [])

            passed = False
            details = f"status={status}"

            # Check Category 1 (In-Scope)
            if case.get("expected_status") == "ANSWERED":
                retrieved_docs = [c["documentId"] for c in citations]
                expected_doc = case["expected_doc"]
                if status == "ANSWERED" and expected_doc in retrieved_docs:
                    passed = True
                    correct_in_scope += 1
                details += f", retrieved={retrieved_docs}, expected={expected_doc}"

            # Check Category 2 (Real-time)
            elif case.get("expected_status") == "OUT_OF_SCOPE":
                if status == "OUT_OF_SCOPE" and len(citations) == 0:
                    passed = True
                    correct_realtime += 1

            # Check Category 3 (Unrelated)
            elif case.get("expected_status") == "NO_EVIDENCE":
                if status == "NO_EVIDENCE":
                    passed = True
                    correct_out_of_scope += 1

            # Check Category 4 (Security / Admin leak prevention)
            elif "forbidden_doc" in case:
                forbidden = case["forbidden_doc"]
                retrieved_docs = [c["documentId"] for c in citations]
                if forbidden not in retrieved_docs:
                    passed = True
                    correct_security += 1
                details += f", forbidden_leaked={forbidden in retrieved_docs}"

            results_summary.append({
                "id": case_id,
                "passed": passed,
                "question": question,
                "status": status,
                "details": details,
            })

    print("\n" + "=" * 80)
    print("VIETNAMESE RAG BENCHMARK EVALUATION RESULTS")
    print("=" * 80)
    for r in results_summary:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{r['id']}] [{mark}]: {r['question'][:50]}... ({r['details']})")

    print("-" * 80)
    print(f"Category 1 (In-Scope FAQ Recall@4): {correct_in_scope}/10 ({correct_in_scope * 10}%)")
    print(f"Category 2 (Realtime Query Protection): {correct_realtime}/5 ({correct_realtime * 20}%)")
    print(f"Category 3 (Out-of-Scope Detection): {correct_out_of_scope}/3 ({correct_out_of_scope * 33.3:.1f}%)")
    print(f"Category 4 (Admin Leak Prevention): {correct_security}/2 ({correct_security * 50}%)")
    print("=" * 80)

    # Assert 100% security against admin leakage and realtime protection
    assert correct_security == 2, "CRITICAL: Admin documents must never leak to regular users!"
    assert correct_realtime == 5, "Realtime intent detection failed!"
    assert correct_in_scope >= 8, f"In-scope recall below target: {correct_in_scope}/10"
