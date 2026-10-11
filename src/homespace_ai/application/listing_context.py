"""Conversation references, never a cache of listing prices or availability."""

import re
from typing import Any
from uuid import UUID

History = list[dict[str, Any]]
LINK = re.compile(r"/rent/([0-9a-fA-F-]{36})")
NEW_SEARCH = re.compile(r"\b(?:tìm|kiếm|tìm kiếm|cho (?:tôi|mình) xem)\b", re.IGNORECASE)
FOLLOWUP = re.compile(
    r"so sánh|nên chọn|chọn .*phòng|chọn .*căn|đề xuất|gợi ý .*phù hợp|"
    r"rẻ nhất|rẻ hơn|tiết kiệm|phù hợp nhất|trong (?:những|các|số)|"
    r"(?:phòng|căn|nhà|tin) (?:này|đó|trên|vừa|thứ|số|\d+)",
    re.IGNORECASE,
)
RECOMMENDATION = re.compile(r"nên chọn|chọn .*phòng|chọn .*căn|đề xuất|rẻ nhất|rẻ hơn|tiết kiệm|phù hợp nhất", re.IGNORECASE)


def valid_ids(values: list[Any]) -> list[str]:
    ids = []
    for value in values:
        try:
            listing_id = str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
        if listing_id not in ids:
            ids.append(listing_id)
    return ids[:5]


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
    if not ids or NEW_SEARCH.search(question) or not FOLLOWUP.search(question):
        return None
    # A prior listing selection must not turn general contract/payment/account help into
    # a recommendation about rooms. Explicit listing ordinals remain valid detail questions.
    if re.search(r"hợp đồng|thanh toán|hoá đơn|hóa đơn|tài khoản|chính sách|điều khoản", question, re.IGNORECASE) and not re.search(
            r"(?:phòng|căn|nhà|tin)\s*(?:thứ\s*|số\s*)?\d+\b", question, re.IGNORECASE):
        return None
    explicit_group = re.search(r"trong (?:những|các|số)|(?:phòng|căn|nhà|tin) (?:này|đó|trên|vừa)|vừa so sánh",
                               question, re.IGNORECASE)
    possible_new_scope = re.search(
        r"phường|xã|đặc khu|quận|huyện|khu vực|thành phố|tỉnh|gần|thay vì|chuyển sang|"
        r"căn hộ|chung cư|nhà nguyên căn|"
        r"\bở\s+(?!(?:\d+|một|hai|ba|bốn|năm)\s+(?:người|mình)\b)[^\d\s]",
        question, re.IGNORECASE,
    )
    if possible_new_scope and not explicit_group:
        # Leave location/category changes to the semantic planner, not a greedy reference rule.
        return None
    ordinals = [int(value) for value in re.findall(
        r"(?:phòng|căn|nhà|tin)\s*(?:thứ\s*|số\s*)?(\d+)\b", question, re.IGNORECASE)]
    # 'so sánh 4 phòng' describes a count; 'phòng 4' refers to an ordinal.
    if ordinals:
        if any(number < 1 or number > len(ids) for number in ordinals):
            return []
        return list(dict.fromkeys(ids[number - 1] for number in ordinals))
    count = re.search(r"so sánh\s+(\d+)\s+(?:phòng|căn|nhà|tin)", question, re.IGNORECASE)
    if count and int(count[1]) != len(ids):
        return []
    return ids


def context_for(ids: list[str]) -> dict[str, Any]:
    return {"version": 1, "listingIds": valid_ids(ids)}
