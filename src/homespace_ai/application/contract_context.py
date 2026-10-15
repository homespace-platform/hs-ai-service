"""Contract conversation references and routing.

contractContext stores only references (ids in display order, the selected id, role and the business date
they were read on). It is never a cache of contract facts: every turn re-reads Core and Core re-checks
the caller's permission. It is kept apart from listingContext so the two ordinal systems cannot mix.
"""

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

History = list[dict[str, Any]]

# Domain words that can point at the caller's own contracts.
DOMAIN = re.compile(
    r"hợp đồng|phụ lục|gia hạn|hoàn cọc|tiền cọc|đặt cọc|hóa đơn|hoá đơn|tiền thuê|trả phòng|"
    r"chấm dứt|kết thúc thuê|tất toán|quyết toán|hết hạn|nợ", re.IGNORECASE)
# Strong signals that the user is asking about their own data, not about how the platform works.
PERSONAL = re.compile(
    r"của tôi|của mình|đang thuê|đang cho thuê|hiện tại|hiện nay|lúc này|bây giờ|hôm nay|"
    r"đã ký (?:đủ|chưa|xong)|ký đủ chưa|bao nhiêu hợp đồng|mấy hợp đồng|có hợp đồng nào|hợp đồng nào|"
    r"hợp đồng (?:đó|này|kia|thứ|số|hd)|sắp hết hạn|sắp đến hạn|còn bao lâu|còn mấy ngày|"
    r"tôi (?:có thể|đã|đang|còn|phải|có|bị|nợ|được)|mình (?:có thể|đã|đang|còn|phải|có|bị|nợ|được)",
    re.IGNORECASE)
GENERIC = re.compile(
    r"như thế nào|thế nào|cách |làm sao|làm thế nào|quy trình|thủ tục|hướng dẫn|là gì|các bước|bước nào|"
    r"cần (?:những )?gì|điều kiện|ra sao", re.IGNORECASE)
NEW_LISTING_SEARCH = re.compile(r"^\s*(?:tìm|kiếm|cho (?:tôi|mình) xem|gợi ý)\b", re.IGNORECASE)
LISTING_ORDINAL = re.compile(r"(?:phòng|căn hộ|căn|nhà|tin)\s*(?:thứ\s*|số\s*)?\d+\b", re.IGNORECASE)
CONTRACT_ORDINAL = re.compile(r"hợp đồng\s*(?:thứ\s*|số\s*)?(\d+)\b", re.IGNORECASE)
WORD_ORDINAL = re.compile(
    r"hợp đồng\s*(?:thứ\s*)?(nhất|đầu tiên|đầu|hai|ba|bốn|tư|năm|cuối cùng|cuối)\b", re.IGNORECASE)
DEMONSTRATIVE = re.compile(r"hợp đồng (?:đó|này|kia|ấy)|\b(?:nó|cái đó|cái này)\b|phụ lục (?:đó|này)", re.IGNORECASE)
FOLLOWUP_TOPIC = re.compile(
    r"nội thất|trang thiết bị|tiện ích|giá|tiền thuê|phí|điện|nước|cọc|hóa đơn|hoá đơn|gia hạn|hết hạn|"
    r"ngày|kỳ|thanh toán|còn bao lâu|đã ký|chấm dứt|phụ lục|điều khoản|chính sách|hoàn", re.IGNORECASE)

_WORDS = {"nhất": 1, "đầu tiên": 1, "đầu": 1, "hai": 2, "ba": 3, "bốn": 4, "tư": 4, "năm": 5,
          "cuối cùng": -1, "cuối": -1}


@dataclass(frozen=True)
class Route:
    kind: str  # CONTRACT | CONTRACT_AND_RAG
    follow_up: bool = False


def valid_ids(values: list[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        try:
            item = str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
        if item not in out:
            out.append(item)
    return out[:5]


def make_context(ids: list[str], numbers: list[str] | None = None, selected: str | None = None,
                 role: str | None = None, reference_date: str | None = None) -> dict[str, Any]:
    clean = valid_ids(ids)
    chosen = str(UUID(selected)) if selected and valid_ids([selected]) else None
    return {"version": 1, "contractIds": clean,
            "contractNumbers": [str(n)[:40] for n in (numbers or [])][:len(clean)],
            "selectedContractId": chosen if chosen in clean else (clean[0] if len(clean) == 1 else None),
            "role": role if role in {"LANDLORD", "TENANT"} else None,
            "referenceDate": reference_date if isinstance(reference_date, str) and
            re.fullmatch(r"\d{4}-\d{2}-\d{2}", reference_date) else None}


def last_context(history: History) -> tuple[dict[str, Any] | None, bool]:
    """(context, immediate). immediate=True when it belongs to the latest assistant message."""
    seen_assistant = False
    for message in reversed(history[-8:]):
        if message.get("role") != "assistant":
            continue
        context = message.get("contractContext")
        if isinstance(context, dict) and isinstance(context.get("contractIds"), list):
            ids = valid_ids(context["contractIds"])
            if ids:
                return make_context(ids, context.get("contractNumbers"), context.get("selectedContractId"),
                                    context.get("role"), context.get("referenceDate")), not seen_assistant
        seen_assistant = True
    return None, False


def classify(question: str, history: History) -> Route | None:
    """Decide whether the caller's own contracts are involved. None leaves the question to Listing/RAG."""
    q = question.strip()
    context, immediate = last_context(history)
    if NEW_LISTING_SEARCH.search(q) and not re.search(r"của tôi|đang thuê|đang cho thuê|đã ký", q, re.IGNORECASE):
        return None
    explicit = bool(CONTRACT_ORDINAL.search(q) or WORD_ORDINAL.search(q)
                    or re.search(r"hợp đồng (?:đó|này|kia|ấy)|phụ lục (?:đó|này)", q, re.IGNORECASE))
    # Loose references ('nó', 'giá bao nhiêu') only continue a contract answer that is the latest assistant turn,
    # so they cannot capture a later listing conversation.
    if context and not LISTING_ORDINAL.search(q) and (
            explicit or (immediate and (DEMONSTRATIVE.search(q) or FOLLOWUP_TOPIC.search(q)))):
        # A follow-up about an already shown contract. Questions that are purely 'how do I' still add RAG.
        return Route("CONTRACT_AND_RAG" if GENERIC.search(q) and re.search(r"gia hạn|ký|chấm dứt|hoàn", q, re.IGNORECASE)
                     else "CONTRACT", follow_up=True)
    if not DOMAIN.search(q):
        return None
    if not PERSONAL.search(q):
        return None  # general knowledge about contracts: the existing RAG path
    return Route("CONTRACT_AND_RAG" if GENERIC.search(q) else "CONTRACT")


def resolve_reference(question: str, context: dict[str, Any] | None) -> tuple[str | None, bool]:
    """(contractId, out_of_range). Resolves only references the application itself stored."""
    if not context:
        return None, False
    ids = context["contractIds"]
    ordinal = CONTRACT_ORDINAL.search(question)
    if ordinal:
        index = int(ordinal.group(1))
        return (ids[index - 1], False) if 1 <= index <= len(ids) else (None, True)
    word = WORD_ORDINAL.search(question)
    if word:
        index = _WORDS[word.group(1).lower()]
        index = len(ids) if index == -1 else index
        return (ids[index - 1], False) if 1 <= index <= len(ids) else (None, True)
    lowered = question.lower()
    for position, number in enumerate(context.get("contractNumbers") or []):
        if number and number.lower() in lowered:
            return ids[position], False
    if context.get("selectedContractId"):
        return context["selectedContractId"], False
    if len(ids) == 1:
        return ids[0], False
    return None, False
