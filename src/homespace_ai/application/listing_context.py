"""Conversation references, never a cache of listing prices or availability."""

import re
from typing import Any
from uuid import UUID
from homespace_ai.application.vietnamese_text import VietnamesePattern, fold_text
from homespace_ai.application.listing_limits import ABSOLUTE_MAX_RESULTS

History = list[dict[str, Any]]
LINK = re.compile(r"/rent/([0-9a-fA-F-]{36})")
NEW_SEARCH = VietnamesePattern(r"\b(?:tìm|kiếm|tìm kiếm|cho (?:tôi|mình) xem)\b")
FOLLOWUP = VietnamesePattern(
    r"so sánh|nên chọn|chọn .*phòng|chọn .*căn|đề xuất|gợi ý .*phù hợp|"
    r"rẻ nhất|rẻ hơn|tiết kiệm|phù hợp nhất|trong (?:những|các|số)|"
    r"(?:phòng|căn|nhà|tin) (?:này|đó|trên|vừa|thứ|số|\d+)",
)
RECOMMENDATION = VietnamesePattern(r"nên chọn|chọn .*phòng|chọn .*căn|đề xuất|rẻ nhất|rẻ hơn|tiết kiệm|phù hợp nhất")


def valid_ids(values: list[Any]) -> list[str]:
    ids = []
    for value in values:
        try:
            listing_id = str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
        if listing_id not in ids:
            ids.append(listing_id)
    return ids[:ABSOLUTE_MAX_RESULTS]


def current_scope(history: History) -> list[str]:
    """Use the last assistant scope; do not mix older search results or user-supplied links.

    Legacy conversations can recover IDs from full assistant Markdown (before truncation).
    Errors don't erase a selection, but an unrelated successful answer or empty search does.
    """
    for message in reversed(history):
        if message.get("role") != "assistant":
            continue
        context = message.get("listingContext")
        if isinstance(context, dict) and isinstance(context.get("listingIds"), list):
            return valid_ids(context["listingIds"])
        if message.get("status") in {"TOOL_UNAVAILABLE", "GENERATION_UNAVAILABLE"}:
            continue
        ids = valid_ids(LINK.findall(message.get("content", "")))
        if ids:
            return ids
        # Legacy failures did not always store status. Never skip successful RAG answers.
        if message.get("status") in {"ANSWERED", "GENERAL_ANSWER", "NO_RESULTS", "NO_EVIDENCE", "OUT_OF_SCOPE"}:
            return []
    return []


def scoped_selection(question: str, history: History) -> list[str] | None:
    """Resolve only unambiguous references locally; semantic/novel requests use the planner.

    [] means an explicit ordinal is outside the displayed set and needs clarification.
    None means this is not an unambiguous scoped follow-up.
    """
    ids = current_scope(history)
    # "Các phòng vừa tìm" refers to the displayed selection, not a command to search again.
    # Folding accents also makes "tiết kiệm" look like the search verb "kiếm".
    # Keep any separate command, e.g. "các phòng vừa tìm không hợp, tìm thêm phòng khác".
    search_command = re.sub(
        r"\b(?:vua|da)(?:\s+moi)?\s+(?:tim(?:\s+kiem)?|kiem)(?:\s+(?:duoc|thay))?\b|\btiet\s+kiem\b",
        "", fold_text(question))
    if not ids or NEW_SEARCH.search(search_command) or not FOLLOWUP.search(question):
        return None
    # A prior listing selection must not turn general contract/payment/account help into
    # a recommendation about rooms. Explicit listing ordinals remain valid detail questions.
    if VietnamesePattern(r"hợp đồng|thanh toán|hoá đơn|hóa đơn|tài khoản|chính sách|điều khoản").search(question) and not VietnamesePattern(
            r"(?:phòng|căn|nhà|tin)\s*(?:thứ\s*|số\s*)?\d+\b").search(question):
        return None
    explicit_group = VietnamesePattern(r"trong (?:những|các|số)|(?:phòng|căn|nhà|tin) (?:này|đó|trên|vừa)|vừa so sánh").search(question)
    possible_new_scope = VietnamesePattern(
        r"phường|xã|đặc khu|quận|huyện|khu vực|thành phố|tỉnh|gần|thay vì|chuyển sang|"
        r"căn hộ|chung cư|nhà nguyên căn|"
        r"\bở\s+(?!(?:\d+|một|hai|ba|bốn|năm)\s+(?:người|mình)\b)[^\d\s]",
    ).search(question)
    if possible_new_scope and not explicit_group:
        # Leave location/category changes to the semantic planner, not a greedy reference rule.
        return None
    ordinals = [int(value) for value in VietnamesePattern(
        r"(?:phòng|căn|nhà|tin)\s*(?:thứ\s*|số\s*)?(\d+)\b").findall(question)]
    # 'so sánh 4 phòng' describes a count; 'phòng 4' refers to an ordinal.
    if ordinals:
        if any(number < 1 or number > len(ids) for number in ordinals):
            return []
        return list(dict.fromkeys(ids[number - 1] for number in ordinals))
    count = VietnamesePattern(r"so sánh\s+(\d+)\s+(?:phòng|căn|nhà|tin)").search(question)
    if count and int(count[1]) != len(ids):
        return []
    return ids


def context_for(ids: list[str]) -> dict[str, Any]:
    return {"version": 1, "listingIds": valid_ids(ids)}
