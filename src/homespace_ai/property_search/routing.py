"""Choose the source of truth for each chat turn, not for the whole session.

A saved search context is useful for follow-ups, but it must never force a
HomeSpace help question through the live-listing tools.
"""

import re
import unicodedata
from enum import Enum

from homespace_ai.property_search.field_qa import is_detail_followup


class QuestionRoute(str, Enum):
    KNOWLEDGE = "knowledge"
    LISTING_SEARCH = "listing_search"
    LISTING_DETAIL = "listing_detail"


def _plain(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    return "".join(char for char in value if unicodedata.category(char) != "Mn")


_LISTING_REFERENCE = (
    "tin nay", "tin do", "tin vua", "tin truoc", "hai tin", "2 tin",
    "phong nay", "phong do", "hai phong", "2 phong", "can nay", "can do",
    "can ho nay", "can ho do", "nha nay", "nha do", "hai can", "2 can",
    "cho do", "cho nay", "trong hai", "trong cac tin", "vua gui", "vua tim",
)
_KNOWLEDGE_TOPIC = (
    "homespace", "nen tang", "ung dung", "tinh nang", "chinh sach",
    "dieu khoan", "quy dinh", "huong dan", "quy trinh", "tai khoan",
    "dang nhap", "dang ky", "kiem duyet", "xac thuc", "bao mat",
    "ho tro khach hang", "bo phan ho tro", "su dung dich vu",
)
_WORKFLOW = (
    "dang tin", "dang bai", "tao tin", "cho thue tren", "dat lich",
    "dat coc", "ky hop dong", "thanh toan", "bao cao tin",
    "lien he chu nha", "tim nha tren", "tim phong tren",
)
_HOW_TO = ("cach ", "lam sao", "nhu the nao", "the nao", "huong dan", "quy trinh")
_SEARCH_CUE = (
    "tim phong", "tim nha", "tim can", "tim tro", "kiem phong", "kiem nha",
    "tim cho o", "kiem cho o", "gan dai hoc",
    "goi y phong", "goi y nha", "goi y can", "phong nao", "nha nao",
    "can nao", "con phong", "con can", "con tin", "them lua chon",
    "bo yeu cau", "bo tieu chi", "mo rong khu vuc", "chuyen sang",
)
_PROPERTY_CUE = (
    "phong tro", "can ho", "nha nguyen can", "chung cu", "studio",
    "gia duoi", "duoi ", "trieu", "tr/", "ban cong", "gac lung",
    "gara", "garage", "go vap", "binh thanh",
)


def route_question(
    question: str,
    *,
    has_search_context: bool,
    has_previous_results: bool,
    changed_ward: bool = False,
    explicit_search_context: bool = False,
) -> QuestionRoute:
    """Route one turn using its intent; previous search state is only a hint.

    Knowledge/workflow questions are checked before detail-field aliases:
    ``chủ nhà đăng tin như thế nào`` must not match the ``owner`` field.
    Explicit references to a *particular* listing still take priority for
    questions such as ``chủ nhà của phòng này là ai``.
    """
    plain = _plain(question.strip())
    if not plain:
        return QuestionRoute.KNOWLEDGE

    refers_to_listing = any(term in plain for term in _LISTING_REFERENCE)
    has_how_to = any(term in plain for term in _HOW_TO)
    is_workflow = any(term in plain for term in _WORKFLOW)
    is_platform_topic = any(term in plain for term in _KNOWLEDGE_TOPIC)
    asks_platform_identity = bool(re.search(
        r"\b(homespace|nen tang|ung dung)\b.*\b(la gi|tinh nang|hoat dong|dung de lam gi)",
        plain,
    ))
    if not refers_to_listing and (
        asks_platform_identity
        or (is_platform_topic and (has_how_to or "tinh nang" in plain))
        or (has_how_to and is_workflow)
        or (is_platform_topic and not any(term in plain for term in _SEARCH_CUE + _PROPERTY_CUE))
    ):
        return QuestionRoute.KNOWLEDGE

    # A new search must not be mistaken for a detail follow-up just because it
    # mentions a field such as price or ends with a question mark.
    fresh_search = bool(re.search(
        r"\b(?:tim|kiem|goi y)\b.{0,35}\b(?:phong|tro|nha|can|cho o)\b",
        plain,
    )) or any(term in plain for term in (
        "bo yeu cau", "bo tieu chi", "mo rong khu vuc", "chuyen sang",
        "them lua chon", "con tin nao khac", "tim tiep",
    ))
    if fresh_search and not refers_to_listing:
        return QuestionRoute.LISTING_SEARCH

    if has_search_context and is_detail_followup(question, has_previous_results, changed_ward):
        return QuestionRoute.LISTING_DETAIL

    if has_search_context and (
        explicit_search_context
        or any(term in plain for term in _SEARCH_CUE)
        or any(term in plain for term in _PROPERTY_CUE)
    ):
        return QuestionRoute.LISTING_SEARCH

    return QuestionRoute.KNOWLEDGE


def knowledge_history(messages: list[dict]) -> list[dict]:
    """Do not inject listing answers into retrieval of platform documents."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "assistant" and messages[index].get("status") == "PROPERTY_SEARCH":
            return messages[index + 1:]
    return messages


def listing_user_history(messages: list[dict]) -> list[str]:
    """Only property-search turns may supply inherited listing filters."""
    return [
        str(message.get("content"))
        for index, message in enumerate(messages[:-1])
        if message.get("role") == "user" and message.get("content")
        and messages[index + 1].get("role") == "assistant"
        and messages[index + 1].get("status") == "PROPERTY_SEARCH"
    ][-8:]
