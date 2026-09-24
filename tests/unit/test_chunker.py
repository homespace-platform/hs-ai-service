class SimpleMockTokenizer:
    """Fast lightweight tokenizer for unit tests counting whitespace words + punctuation."""
    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        import re
        tokens = re.findall(r"\w+|[^\w\s]", text, re.UNICODE)
        return list(range(len(tokens)))


def test_chunker_basic_headings():
    from homespace_ai.knowledge.ingestion.chunker import MarkdownChunker

    tokenizer = SimpleMockTokenizer()
    chunker = MarkdownChunker(
        tokenizer=tokenizer,
        target_tokens=50,
        overlap_tokens=10,
        max_tokens=100,
    )

    markdown = """# Giới thiệu chung

Đây là đoạn mở đầu về nền tảng HomeSpace.

## Tính năng tìm kiếm

Người thuê có thể tìm kiếm theo khu vực, mức giá và tiện ích.
Bộ lọc cho phép lọc theo số phòng ngủ.

## Hợp đồng điện tử

HomeSpace hỗ trợ ký hợp đồng điện tử thông qua VNPT SmartCA.
"""
    chunks = chunker.chunk_document(
        document_id="hs-test-overview",
        version="0.1.0",
        title="Giới thiệu chung",
        visibility="public",
        locale="vi-VN",
        raw_markdown=markdown,
    )

    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.token_count <= 100
        assert chunk.passage_text.startswith("passage: ")
        assert chunk.heading_path
        assert chunk.metadata["document_id"] == "hs-test-overview"
        assert chunk.metadata["visibility"] == "public"


def test_chunker_large_block_split_with_overlap():
    from homespace_ai.knowledge.ingestion.chunker import MarkdownChunker

    tokenizer = SimpleMockTokenizer()
    chunker = MarkdownChunker(
        tokenizer=tokenizer,
        target_tokens=20,
        overlap_tokens=5,
        max_tokens=30,
    )

    long_text = (
        "Câu thứ nhất nói về quy trình thuê. "
        "Câu thứ hai giải thích về tiền cọc. "
        "Câu thứ ba nêu rõ thời hạn thanh toán. "
        "Câu thứ tư hướng dẫn nhận phòng. "
        "Câu thứ năm lưu ý về an toàn."
    )

    chunks = chunker.chunk_document(
        document_id="hs-long-test",
        version="0.1.0",
        title="Quy trình dài",
        visibility="public",
        locale="vi-VN",
        raw_markdown=f"# Mục chính\n\n{long_text}",
    )

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.token_count <= 40
