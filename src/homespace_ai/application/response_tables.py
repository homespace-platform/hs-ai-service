"""Small GFM tables for verified read-only results; cells never execute customer markup."""
import re
from html import escape
from collections.abc import Iterable, Sequence


def _cell(value: object) -> str:
    if value is None or value == "":
        return "Chưa có thông tin"
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = escape(text, quote=False)
    return re.sub(r"([\\`*_{}\[\]|~])", r"\\\1", text) or "Chưa có thông tin"


def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    data = list(rows)
    if not data:
        return ""
    if not headers or any(len(row) != len(headers) for row in data):
        raise ValueError("Every table row must match its headers")
    line = lambda row: "| " + " | ".join(_cell(value) for value in row) + " |"
    return "\n".join([line(headers), "| " + " | ".join("---" for _ in headers) + " |", *(line(row) for row in data)])
