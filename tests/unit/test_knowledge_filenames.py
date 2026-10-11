import pytest
from fastapi import HTTPException

from homespace_ai.knowledge.filenames import normalize_filename


def test_filename_identity_is_case_insensitive_and_unicode_normalized():
    assert normalize_filename("  FAQ.MD  ") == "faq.md"
    assert normalize_filename("Hướng dẫn.md") == normalize_filename("Hướng dẫn.md")


@pytest.mark.parametrize("filename", ["../secret.md", "C:\\secret.md", "a/b.md", "a\x00.md", "x.txt", "a" * 256 + ".md"])
def test_filename_cannot_contain_paths_control_chars_or_non_markdown(filename):
    with pytest.raises(HTTPException):
        normalize_filename(filename)
