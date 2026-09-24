import pytest
from fastapi import HTTPException
from homespace_ai.knowledge.ingestion.parser import MarkdownDocumentParser


def test_parser_valid_markdown():
    parser = MarkdownDocumentParser()
    sample = """---
document_id: test-doc-1
title: Test Document Title
audience: [tenant, landlord]
visibility: public
status: draft
version: 1.0.0
locale: vi-VN
category: General
---

# Test Heading

This is sample content for testing.
"""
    parsed = parser.parse("test.md", sample.encode("utf-8"))
    assert parsed.document_id == "test-doc-1"
    assert parsed.title == "Test Document Title"
    assert parsed.visibility == "public"
    assert parsed.audience == ["tenant", "landlord"]
    assert parsed.locale == "vi-VN"
    assert parsed.version == "1.0.0"
    assert parsed.category == "General"
    assert "This is sample content" in parsed.raw_markdown
    assert len(parsed.content_hash) == 64


def test_parser_rejects_non_md_extension():
    parser = MarkdownDocumentParser()
    with pytest.raises(HTTPException) as exc:
        parser.parse("test.txt", b"hello")
    assert exc.value.status_code == 400
    assert "Only .md" in exc.value.detail


def test_parser_rejects_oversized_file():
    parser = MarkdownDocumentParser()
    big_content = b"a" * 2000
    with pytest.raises(HTTPException) as exc:
        parser.parse("big.md", big_content, max_size_bytes=1000)
    assert exc.value.status_code == 413


def test_parser_rejects_missing_front_matter():
    parser = MarkdownDocumentParser()
    content = b"# Just heading without front matter"
    with pytest.raises(HTTPException) as exc:
        parser.parse("no_front_matter.md", content)
    assert exc.value.status_code == 400


def test_parser_rejects_invalid_visibility():
    parser = MarkdownDocumentParser()
    sample = """---
document_id: test-doc-invalid
title: Invalid Visibility
visibility: superuser
---

Content
"""
    with pytest.raises(HTTPException) as exc:
        parser.parse("invalid.md", sample.encode("utf-8"))
    assert exc.value.status_code == 400
    assert "visibility" in exc.value.detail.lower()


def test_parser_serializes_yaml_date_for_jsonb():
    parser = MarkdownDocumentParser()
    content = b"""---
document_id: test-dated
title: Dated document
last_reviewed: 2026-09-25
---

Content
"""
    parsed = parser.parse("dated.md", content)
    assert parsed.metadata["last_reviewed"] == "2026-09-25"


def test_parser_rejects_document_id_with_path_separator():
    parser = MarkdownDocumentParser()
    content = b"""---
document_id: ../private
title: Invalid ID
---

Content
"""
    with pytest.raises(HTTPException) as exc:
        parser.parse("invalid.md", content)
    assert exc.value.status_code == 400
