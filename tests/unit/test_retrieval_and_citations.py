import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from homespace_ai.application.use_cases.ask_use_cases import AskResult, AskUseCases
from homespace_ai.models.knowledge import KnowledgeChunk


# --- 1. Unit Tests for Citation Parsing & Verification ---

def test_citation_parsing_valid_sources():
    use_cases = AskUseCases(
        session=None, settings=None, embedder=None, generative_client=None
    )
    context_chunks = [
        {
            "source_id": "C1",
            "chunk_id": "chunk-111",
            "document_id": "hs-knowledge-overview",
            "title": "Giới thiệu HomeSpace",
            "heading": "1. Tổng quan nền tảng",
            "version": "0.1.0",
            "content": "HomeSpace là nền tảng công nghệ kết nối trực tiếp chủ nhà và người thuê bất động sản minh bạch.",
            "similarity": 0.88,
        },
        {
            "source_id": "C2",
            "chunk_id": "chunk-222",
            "document_id": "hs-policy-terms",
            "title": "Điều khoản sử dụng",
            "heading": "4. Giao dịch người dùng",
            "version": "0.1.0",
            "content": "Người dùng tự thỏa thuận các điều khoản thuê theo pháp luật hiện hành.",
            "similarity": 0.65,
        },
    ]

    raw_answer = (
        "HomeSpace là nền tảng kết nối trực tiếp người thuê và chủ nhà minh bạch.\n\n"
        "TRÍCH DẪN: [C1]"
    )

    clean_answer, verified = use_cases._parse_and_verify_citations(raw_answer, context_chunks)

    assert "TRÍCH DẪN:" not in clean_answer
    assert "HomeSpace là nền tảng kết nối" in clean_answer
    assert len(verified) == 1
    assert verified[0]["documentId"] == "hs-knowledge-overview"
    assert verified[0]["heading"] == "1. Tổng quan nền tảng"
    assert verified[0]["snippet"].startswith("HomeSpace là nền tảng")


def test_citation_parsing_rejects_hallucinated_sources():
    use_cases = AskUseCases(
        session=None, settings=None, embedder=None, generative_client=None
    )
    context_chunks = [
        {
            "source_id": "C1",
            "chunk_id": "chunk-111",
            "document_id": "hs-knowledge-overview",
            "title": "Giới thiệu HomeSpace",
            "heading": "1. Tổng quan",
            "version": "0.1.0",
            "content": "Nội dung giới thiệu...",
            "similarity": 0.85,
        }
    ]

    # Model hallucinated [C2] and [C99] which are not in context_chunks (length 1)
    raw_answer = "Câu trả lời.\n\nTRÍCH DẪN: [C2], [C99]"
    clean_answer, verified = use_cases._parse_and_verify_citations(raw_answer, context_chunks)

    assert len(verified) == 0


def test_citation_parsing_no_evidence_indicator():
    use_cases = AskUseCases(
        session=None, settings=None, embedder=None, generative_client=None
    )
    context_chunks = [
        {
            "source_id": "C1",
            "chunk_id": "chunk-111",
            "document_id": "hs-policy-terms",
            "title": "Điều khoản sử dụng",
            "heading": "Điều khoản chung",
            "version": "0.1.0",
            "content": "Điều khoản sử dụng chung...",
            "similarity": 0.60,
        }
    ]

    raw_answer = "Hiện tại tài liệu của HomeSpace chưa có thông tin về tính năng này.\n\nTRÍCH DẪN: KHÔNG"
    clean_answer, verified = use_cases._parse_and_verify_citations(raw_answer, context_chunks)

    assert len(verified) == 0
    assert "chưa có thông tin" in clean_answer.lower()


# --- 2. Unit Tests for Heading Deduplication & Document Diversity in Fusion ---

def test_heading_deduplication_and_diversity():
    from homespace_ai.repositories.chunk_repo import ChunkRepository

    # Create dummy repository with mock session
    repo = ChunkRepository(session=MagicMock())

    # Create 3 chunks from same heading and same doc
    c1 = KnowledgeChunk(
        id=uuid.uuid4(),
        document_id="hs-policy-terms",
        chunk_index=0,
        heading_path="Điều khoản chung > 1. Phạm vi",
        content="Chunk 1",
        token_count=100,
        chunk_metadata={"title": "Điều khoản sử dụng", "category": "policy"},
    )
    c2 = KnowledgeChunk(
        id=uuid.uuid4(),
        document_id="hs-policy-terms",
        chunk_index=1,
        heading_path="Điều khoản chung > 1. Phạm vi",
        content="Chunk 2 (same heading)",
        token_count=100,
        chunk_metadata={"title": "Điều khoản sử dụng", "category": "policy"},
    )
    c3 = KnowledgeChunk(
        id=uuid.uuid4(),
        document_id="hs-policy-terms",
        chunk_index=2,
        heading_path="Điều khoản chung > 2. Trách nhiệm",
        content="Chunk 3 (different heading)",
        token_count=100,
        chunk_metadata={"title": "Điều khoản sử dụng", "category": "policy"},
    )
    c4 = KnowledgeChunk(
        id=uuid.uuid4(),
        document_id="hs-knowledge-overview",
        chunk_index=0,
        heading_path="Giới thiệu > 1. Tổng quan",
        content="Chunk 4 overview",
        token_count=100,
        chunk_metadata={"title": "Giới thiệu HomeSpace", "category": "overview"},
    )

    vector_candidates = [(c1, 0.82), (c2, 0.81), (c3, 0.79), (c4, 0.78)]
    lexical_candidates = [(c4, 1.5), (c1, 1.2)]

    # Mock internal methods
    repo.search_chunks = AsyncMock(return_value=vector_candidates)
    repo.search_chunks_lexical = AsyncMock(return_value=lexical_candidates)

    import asyncio
    results = asyncio.run(
        repo.search_chunks_hybrid(
            query_text="HomeSpace là nền tảng gì và dành cho ai?",
            query_vector=[0.1] * 384,
            top_k=4,
        )
    )

    # 1. Overview chunk (c4) must be ranked #1 due to overview intent boost + lexical match
    assert results[0][0].document_id == "hs-knowledge-overview"

    # 2. Duplicate heading (c2) must have been filtered out (only c1 or c2 kept, not both)
    returned_cids = [r[0].id for r in results]
    assert not (c1.id in returned_cids and c2.id in returned_cids)


# --- 3. 30-Question Vietnamese Evaluation Benchmark ---

BENCHMARK_30_QUESTIONS = [
    # 1. Overview (4 questions)
    {"q": "HomeSpace là nền tảng gì và dành cho những ai sử dụng?", "cat": "overview", "expect_doc": "hs-knowledge-overview"},
    {"q": "Mục tiêu và vai trò chính của HomeSpace trong thuê nhà là gì?", "cat": "overview", "expect_doc": "hs-knowledge-overview"},
    {"q": "Những đối tượng khách hàng nào phù hợp với HomeSpace?", "cat": "overview", "expect_doc": "hs-knowledge-overview"},
    {"q": "HomeSpace khác gì so với các trang tin rao vặt bất động sản thông thường?", "cat": "overview", "expect_doc": "hs-knowledge-overview"},

    # 2. Listing Management (4 questions)
    {"q": "Chủ nhà đăng tin cho thuê trên HomeSpace như thế nào?", "cat": "listing", "expect_doc": "hs-knowledge-listing-management"},
    {"q": "Quy trình kiểm duyệt tin đăng của chủ nhà diễn ra trong bao lâu?", "cat": "listing", "expect_doc": "hs-knowledge-listing-management"},
    {"q": "Làm thế nào để chỉnh sửa hoặc tạm dừng tin đăng cho thuê?", "cat": "listing", "expect_doc": "hs-knowledge-listing-management"},
    {"q": "Hình ảnh và thông tin phòng trọ cần đáp ứng tiêu chuẩn gì?", "cat": "listing", "expect_doc": "hs-knowledge-listing-management"},

    # 3. Appointments (4 questions)
    {"q": "Làm thế nào để đặt lịch xem nhà trực tiếp trên HomeSpace?", "cat": "appointment", "expect_doc": "hs-knowledge-appointments"},
    {"q": "Người thuê có thể hủy hoặc đổi giờ hẹn xem phòng không?", "cat": "appointment", "expect_doc": "hs-knowledge-appointments"},
    {"q": "Chủ nhà xác nhận lịch hẹn xem nhà qua kênh nào?", "cat": "appointment", "expect_doc": "hs-knowledge-appointments"},
    {"q": "Nếu chủ nhà không đến điểm hẹn xem nhà thì xử lý ra sao?", "cat": "appointment", "expect_doc": "hs-knowledge-appointments"},

    # 4. Payments & Deposit (4 questions)
    {"q": "Quy trình thanh toán tiền cọc giữ chỗ trên HomeSpace như thế nào?", "cat": "payment", "expect_doc": "hs-knowledge-payments"},
    {"q": "Tiền cọc được giữ an toàn bởi hệ thống hay chuyển thẳng cho chủ nhà?", "cat": "payment", "expect_doc": "hs-knowledge-payments"},
    {"q": "Khi phát sinh sự cố chuyển khoản cọc không nhận được thì liên hệ ai?", "cat": "payment", "expect_doc": "hs-knowledge-payments"},
    {"q": "Chính sách hoàn cọc nếu không thể ký hợp đồng do lỗi của chủ nhà?", "cat": "payment", "expect_doc": "hs-knowledge-payments"},

    # 5. Contracts (4 questions)
    {"q": "Quy trình ký hợp đồng điện tử trên HomeSpace gồm các bước nào?", "cat": "contract", "expect_doc": "hs-knowledge-contracts"},
    {"q": "Hợp đồng thuê điện tử có giá trị pháp lý không?", "cat": "contract", "expect_doc": "hs-knowledge-contracts"},
    {"q": "Làm thế nào để ký phụ lục hợp đồng khi gia hạn thời gian thuê?", "cat": "contract", "expect_doc": "hs-knowledge-contracts"},
    {"q": "Chấm dứt hợp đồng trước hạn cần báo trước bao nhiêu ngày?", "cat": "contract", "expect_doc": "hs-knowledge-contracts"},

    # 6. Terms & Policies (4 questions)
    {"q": "Các hành vi nào bị nghiêm cấm trên nền tảng HomeSpace?", "cat": "policy", "expect_doc": "hs-policy-terms-of-service"},
    {"q": "Chính sách bảo mật thông tin cá nhân của HomeSpace như thế nào?", "cat": "policy", "expect_doc": "hs-policy-privacy"},
    {"q": "Tin đăng vi phạm bản quyền hình ảnh sẽ bị xử lý ra sao?", "cat": "policy", "expect_doc": "hs-policy-moderation"},
    {"q": "Trách nhiệm của chủ nhà khi đăng thông tin không đúng thực tế?", "cat": "policy", "expect_doc": "hs-policy-terms-of-service"},

    # 7. Out-of-Scope / Personal dynamic queries (3 questions)
    {"q": "Tin đăng phòng trọ của tôi đã được admin duyệt chưa?", "cat": "out_of_scope", "expect_doc": None},
    {"q": "Hóa đơn tiền thuê tháng này của tôi cần thanh toán bao nhiêu?", "cat": "out_of_scope", "expect_doc": None},
    {"q": "Lịch hẹn xem nhà của tôi ngày mai lúc mấy giờ?", "cat": "out_of_scope", "expect_doc": None},

    # 8. Admin-only confidentiality isolation (3 questions)
    {"q": "Quy trình vận hành mật của quản trị viên và xử lý tài khoản gian lận?", "cat": "admin_only", "expect_doc": "hs-knowledge-admin-operations"},
    {"q": "Hướng dẫn can thiệp hệ thống và khóa tài khoản dành riêng cho Admin?", "cat": "admin_only", "expect_doc": "hs-knowledge-admin-operations"},
    {"q": "Tài liệu kiểm toán bảo mật nội bộ mức quản trị?", "cat": "admin_only", "expect_doc": "hs-knowledge-admin-operations"},
]


def test_vietnamese_benchmark_dataset_integrity():
    assert len(BENCHMARK_30_QUESTIONS) == 30
    cats = {item["cat"] for item in BENCHMARK_30_QUESTIONS}
    assert cats == {
        "overview", "listing", "appointment", "payment",
        "contract", "policy", "out_of_scope", "admin_only"
    }


def test_out_of_scope_detection_on_benchmark():
    use_cases = AskUseCases(
        session=None, settings=None, embedder=None, generative_client=None
    )
    out_of_scope_questions = [
        item for item in BENCHMARK_30_QUESTIONS if item["cat"] == "out_of_scope"
    ]
    for item in out_of_scope_questions:
        assert use_cases.is_realtime_or_personal_query(item["q"]), (
            f"Expected '{item['q']}' to be classified as out-of-scope personal query"
        )
