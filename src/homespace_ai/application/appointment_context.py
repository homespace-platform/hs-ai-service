"""Viewing-appointment conversation references and routing.

appointmentContext stores only references (ids in display order, the selected id, the resolved filters and the
page state). It is never a cache of appointment facts: every turn re-reads Core, and Core re-checks the caller's
permission. It is kept apart from listingContext and contractContext so the ordinal systems cannot mix.

All matching runs on diacritic-folded text so that "lịch hẹn" and "lich hen" route identically. Folding makes
"tôi", "tối" and "tới" identical, so patterns never rely on those words alone.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

History = list[dict[str, Any]]

MAX_IDS = 10


def fold(text: str) -> str:
    """Lower-case, strip diacritics (đ -> d), collapse whitespace."""
    lowered = unicodedata.normalize("NFC", text or "").lower().replace("đ", "d")
    stripped = "".join(c for c in unicodedata.normalize("NFD", lowered) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", stripped).strip()


# Words that can point at viewing appointments. Bare 'ngay', 'nha', 'phong', 'het han' are deliberately absent.
DOMAIN = re.compile(
    r"\blich\b(?!\s+su\b)|\bdat lich\b|\bhen xem\b|\bxem nha\b|\bbuoi xem\b|\bbuoi hen\b|\bcuoc hen\b|"
    r"\byeu cau (?:xem nha|dat lich)\b|\btiep khach\b")
# Strong appointment words (a bare 'lich' next to contract words is not enough).
STRONG_DOMAIN = re.compile(r"\blich (?:hen|xem|di xem|tiep khach|dat)|\bdat lich\b|\bbuoi xem\b|\blich cua toi\b|\bxem nha\b")
OTHER_DOMAIN = re.compile(r"hop dong|hoa don|phu luc|tien coc|gia han|thanh toan|tat toan|quyet toan")
PERSONAL = re.compile(
    r"\bcua toi\b|\bcua minh\b|\btoi (?:co|da|dang|con|bi|duoc|phai|se|muon|can)\b|\bminh (?:co|da|dang|con|bi|duoc|phai|se|muon|can)\b|"
    r"\bbao nhieu\b|\bmay (?:lich|buoi|yeu cau|khach|nguoi)\b|\blich nao\b|\bnhung lich\b|\bcac lich\b|\bkhach nao\b|"
    r"\bai (?:dat|hen|xin|yeu cau)\b|\bco (?:lich|buoi|yeu cau)\b|\bsap toi\b|\bsap den\b|\btiep theo\b|\bgan nhat\b|\bcho (?:toi|minh)\b|"
    r"\bchu nha (?:da|co|chua|dang)\b|\bkhach (?:da|co|chua|dang)\b|\bchua\s*\??$")
TIME = re.compile(
    r"\bhom nay\b|\bngay mai\b|\bngay kia\b|\bmai\b|\btuan nay\b|\btuan sau\b|\btuan toi\b|\bcuoi tuan\b|\bthang nay\b|"
    r"\bsang mai\b|\bchieu mai\b|\btoi nay\b|\bthu (?:hai|ba|tu|nam|sau|bay|[2-7])\b|\bchu nhat\b|"
    r"\b\d{1,2}\s*(?:h|gio)\b|\b\d{1,2}/\d{1,2}\b")
GENERIC = re.compile(
    r"nhu the nao|the nao|\bcach\b|lam sao|lam the nao|quy trinh|thu tuc|huong dan|\bla gi\b|cac buoc|"
    r"can (?:nhung )?gi\b|dieu kien|ra sao|\bchinh sach\b|mat (?:bao lau|may)")
NEW_LISTING_SEARCH = re.compile(r"^\s*(?:tim|kiem|goi y|cho (?:toi|minh) xem)\b")

ORDINAL_DIGIT = re.compile(r"\b(?:lich|buoi|yeu cau|cai)\s*(?:thu\s*|so\s*)?(\d{1,2})\b")
_WORDS = {"nhat": 1, "dau tien": 1, "dau": 1, "hai": 2, "ba": 3, "bon": 4, "tu": 4, "nam": 5, "sau": 6, "bay": 7,
          "tam": 8, "chin": 9, "muoi": 10, "cuoi cung": -1, "cuoi": -1}
_WORD_PATTERN = "|".join(sorted(_WORDS, key=len, reverse=True))
ORDINAL_WORD = re.compile(rf"\b(?:lich|buoi|yeu cau|cai)\s*(?:thu\s*)?({_WORD_PATTERN})\b(?!\s*(?:tuan|ngay|thang|nay|toi|vao|gio))")
DEMONSTRATIVE = re.compile(r"\b(?:lich|buoi|yeu cau|cuoc hen) (?:do|nay|kia|ay)\b|\bcai do\b|\bcai nay\b|\bno\b")
PAGING = re.compile(r"\bxem tiep\b|\btrang (?:sau|tiep|ke)\b|\btiep theo nua\b|\bcon nua khong\b|\bxem them\b|\bthem nua\b|\bcon lich nao khac\b")
FOLLOWUP_TOPIC = re.compile(
    r"\bo dau\b|\bdia chi\b|\bmay gio\b|\bluc nao\b|\bghi chu\b|\bly do\b|\btrang thai\b|\bchi tiet\b|\bai dat\b|"
    r"\bnguoi thue\b|\bkhach\b|\bda duoc\b|\bcon hieu luc\b|\bdoi\b|\bhuy\b|\bduyet\b|\bcon (?:ngay|hom|tuan|thang|cuoi)|"
    r"\bthi sao\b|\bcon (?:lich|buoi)\b|\bsao\b")
# Every alternative names viewing/booking explicitly: 'còn chỗ gửi xe' or 'còn trống' alone is a listing question.
AVAILABILITY = re.compile(
    r"\bxem (?:nha |phong |can )?duoc (?:khong|chua|nhe|luc|vao|thu|ngay|hom|sang|chieu|toi|mai|cuoi|tuan|\d)|"
    r"\bxem (?:nha |phong |can )?duoc$|\bco the xem (?:nha|phong|can|vao|luc|ngay|hom|sang|chieu)\b|"
    r"\bdat (?:duoc )?lich (?:duoc )?(?:khong|chua)\b|\bkhung gio\b|\bslot\b|\bgio nao xem\b|"
    r"\bxem (?:luc|vao) (?:nao|gio nao)\b|\bkip xem\b|\bhen xem duoc\b|\bnhan lich\b|"
    r"\blich (?:xem )?con trong\b|\bcon (?:lich|slot|khung gio)\b")
LISTING_REF = re.compile(
    r"\b(?:phong|can ho|can|nha|tin|can nha)\s*(?:thu\s*|so\s*)?(\d{1,2})\b")
LISTING_WORD_REF = re.compile(rf"\b(?:phong|can ho|can|nha|tin)\s*(?:thu\s*)?({_WORD_PATTERN})\b")
LISTING_DEMONSTRATIVE = re.compile(r"\b(?:phong|can ho|can|nha|tin|can nha) (?:nay|do|kia|vua roi|tren)\b")

# ---- actions the assistant must never perform (it explains and guides to the screens instead)
_ACTION_VERBS: list[tuple[str, str]] = [
    ("CANCEL", r"huy"),
    ("APPROVE", r"duyet|chap nhan|dong y|xac nhan"),
    ("REJECT", r"tu choi"),
    ("COMPLETE", r"hoan thanh|danh dau hoan thanh|ket thuc buoi"),
    ("REQUEST_RESCHEDULE", r"doi lich|doi gio|doi sang|doi lai|doi buoi|doi ngay|de xuat (?:doi|gio|lich moi)|xin doi|doi thoi gian"),
    ("BOOK", r"dat lich|dat cho|hen lich|hen xem|book|dang ky xem|tao lich|tao yeu cau"),
]
_ACTION_PATTERN = re.compile(
    r"\b(" + "|".join(f"(?P<{name}>{pattern})" for name, pattern in _ACTION_VERBS) + r")\b")
_NOT_ACTION_BEFORE = {"da", "bi", "duoc", "chua", "khong", "se", "nao", "bao", "cho", "chu", "khach", "thue"}
# a third party doing the thing ("chu nha da xac nhan chua") is a status question, not a request to act
_THIRD_PARTY = re.compile(r"\b(?:ai|chu nha|khach|nguoi thue|ben kia)\b")


@dataclass(frozen=True)
class Route:
    kind: str  # APPOINTMENT | APPOINTMENT_AND_RAG | AVAILABILITY
    follow_up: bool = False
    action: str | None = None


def detect_action(folded: str) -> str | None:
    """Request-to-act detection. Returns a Core action code or BOOK, never anything that gets executed."""
    for match in _ACTION_PATTERN.finditer(folded):
        name = next(key for key in (n for n, _ in _ACTION_VERBS) if match.group(key))
        before = folded[:match.start()].split()
        prev = before[-1] if before else ""
        if prev in {"da", "bi", "duoc", "chua", "khong"}:
            continue  # status wording: 'lịch đã hủy', 'lịch bị từ chối'
        if _THIRD_PARTY.search(" ".join(before[-3:])):
            continue  # someone else is the subject: 'khách đặt lịch', 'chủ nhà đã xác nhận'
        if name == "REQUEST_RESCHEDULE" and (prev in {"lich", "co", "yeu", "cau"} or re.search(r"\bnao\b|\bnhung\b", folded)):
            continue  # 'lịch đề xuất đổi giờ nào' names appointments that have a proposal; it asks nothing to be done
        after = folded[match.end():].strip()
        # the verb must be about an appointment, not about e.g. 'hủy hợp đồng' or 'xác nhận email'
        if name == "BOOK" or re.match(r"(?:giup |dum |cho toi |cho minh |lai |luon |cai |buoi |cac |tat ca |het |yeu cau )*"
                                      r"(?:lich|buoi|yeu cau|cuoc hen|\d)", after) or name in {"REQUEST_RESCHEDULE"} or (
                name == "COMPLETE" and re.match(r"(?:buoi|lich)", after)):
            return name
        if STRONG_DOMAIN.search(folded) and name in {"CANCEL", "APPROVE", "REJECT", "COMPLETE"} and prev not in _NOT_ACTION_BEFORE:
            return name
    return None


def valid_ids(values: list[Any], limit: int = MAX_IDS) -> list[str]:
    out: list[str] = []
    for value in values:
        try:
            item = str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
        if item not in out:
            out.append(item)
    return out[:limit]


_ALLOWED_FILTER_KEYS = ("status", "datePreset", "dateFrom", "dateTo", "timeFrom", "timeTo", "listingId",
                        "rescheduleRequested", "timePhase", "q", "sort")


def _clean_filters(filters: Any) -> dict[str, Any]:
    if not isinstance(filters, dict):
        return {}
    clean: dict[str, Any] = {}
    for key in _ALLOWED_FILTER_KEYS:
        value = filters.get(key)
        if value in (None, "", []):
            continue
        if key in {"status", "timePhase"}:
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                clean[key] = [v[:20] for v in value[:6]]
        elif key == "rescheduleRequested":
            if isinstance(value, bool):
                clean[key] = value
        elif key == "listingId":
            ids = valid_ids([value], 1)
            if ids:
                clean[key] = ids[0]
        elif isinstance(value, str):
            clean[key] = value[:60]
    return clean


def make_context(ids: list[str], selected: str | None = None, role: str | None = None,
                 filters: dict[str, Any] | None = None, page: dict[str, Any] | None = None,
                 read_date: str | None = None) -> dict[str, Any]:
    clean = valid_ids(ids)
    chosen = str(UUID(selected)) if selected and valid_ids([selected]) else None
    page_state = None
    if isinstance(page, dict):
        try:
            page_state = {"page": int(page["page"]), "size": int(page["size"]),
                          "totalPages": int(page["totalPages"]), "total": int(page.get("total", 0)),
                          "hasNext": bool(page["hasNext"])}
        except (KeyError, TypeError, ValueError):
            page_state = None
    return {"version": 1, "appointmentIds": clean,
            "selectedAppointmentId": chosen if chosen in clean else (clean[0] if len(clean) == 1 else None),
            "role": role if role in {"OWNER", "RENTER", "BOTH"} else None,
            "filters": _clean_filters(filters), "page": page_state,
            "readDate": read_date if isinstance(read_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", read_date) else None}


def last_context(history: History) -> tuple[dict[str, Any] | None, bool]:
    """(context, immediate). immediate=True when it belongs to the latest assistant message."""
    seen_assistant = False
    for message in reversed(history[-8:]):
        if message.get("role") != "assistant":
            continue
        context = message.get("appointmentContext")
        if isinstance(context, dict) and isinstance(context.get("appointmentIds"), list):
            ids = valid_ids(context["appointmentIds"])
            return make_context(ids, context.get("selectedAppointmentId"), context.get("role"),
                                context.get("filters"), context.get("page"), context.get("readDate")), not seen_assistant
        seen_assistant = True
    return None, False


def listing_scope(history: History) -> list[str]:
    from homespace_ai.application.listing_context import current_scope
    return current_scope(history)


def _is_availability_question(folded: str) -> bool:
    return bool(AVAILABILITY.search(folded))


def _has_listing_reference(folded: str) -> bool:
    return bool(LISTING_REF.search(folded) or LISTING_WORD_REF.search(folded) or LISTING_DEMONSTRATIVE.search(folded))


def classify(question: str, history: History) -> Route | None:
    """Decide whether the caller's own viewing appointments (or a listing's viewing availability) are involved.
    None leaves the question to Contract/Listing/RAG."""
    folded = fold(question)
    if not folded:
        return None
    context, immediate = last_context(history)
    scope = listing_scope(history)
    appointment_words = bool(DOMAIN.search(folded))
    if OTHER_DOMAIN.search(folded) and not STRONG_DOMAIN.search(folded):
        return None  # contract / invoice wording belongs to the Contract module
    action = detect_action(folded) if (appointment_words or (immediate and context)) else None
    if action and not appointment_words and not (immediate and context and (
            DEMONSTRATIVE.search(folded) or ORDINAL_DIGIT.search(folded) or ORDINAL_WORD.search(folded))):
        action = None
    personal_markers = bool(PERSONAL.search(folded) or TIME.search(folded))
    if action and GENERIC.search(folded) and not personal_markers and not (immediate and context):
        action = None  # 'Đặt lịch xem nhà như thế nào?' is a how-to question for RAG, not a request to act
    if NEW_LISTING_SEARCH.search(folded) and not PERSONAL.search(folded) and not action:
        return None
    # ---- availability of a listing the user was just shown / just named
    if _is_availability_question(folded) and scope and not (
            context and immediate and re.search(r"\blich\b", folded) and not _has_listing_reference(folded)):
        if _has_listing_reference(folded) or len(scope) == 1 or TIME.search(folded):
            if not (GENERIC.search(folded) and not _has_listing_reference(folded)):
                return Route("AVAILABILITY", follow_up=True, action="BOOK" if action == "BOOK" else None)
    # ---- follow-up on an appointment answer that is the latest assistant message
    if context and immediate and context["appointmentIds"] is not None:
        listing_only = _has_listing_reference(folded) and not appointment_words
        if not listing_only and not OTHER_DOMAIN.search(folded):
            explicit = bool(ORDINAL_DIGIT.search(folded) or ORDINAL_WORD.search(folded) or DEMONSTRATIVE.search(folded)
                            or PAGING.search(folded))
            short_topic = len(folded.split()) <= 9 and (FOLLOWUP_TOPIC.search(folded) or TIME.search(folded))
            if explicit or short_topic or action:
                return Route("APPOINTMENT", follow_up=True, action=action)
    if not appointment_words:
        return None
    combined = bool(GENERIC.search(folded)) and personal_markers
    if action:
        return Route("APPOINTMENT_AND_RAG" if combined else "APPOINTMENT", action=action)
    if not personal_markers:
        return None  # general knowledge about viewing appointments: the existing RAG path
    return Route("APPOINTMENT_AND_RAG" if combined else "APPOINTMENT")


@dataclass(frozen=True)
class Reference:
    appointment_id: str | None = None
    out_of_range: bool = False
    ambiguous: bool = False


def resolve_reference(question: str, context: dict[str, Any] | None) -> Reference:
    """Resolves only references the application itself stored (ids in display order)."""
    if not context or not context["appointmentIds"]:
        return Reference()
    folded = fold(question)
    ids = context["appointmentIds"]
    offset = 0
    page = context.get("page")
    if page and page.get("page", 1) > 1:
        offset = (page["page"] - 1) * page["size"]  # numbering continues across pages
    number: int | None = None
    digit = ORDINAL_DIGIT.search(folded)
    word = ORDINAL_WORD.search(folded)
    if digit:
        number = int(digit.group(1))
    elif word:
        number = _WORDS[word.group(1)]
        if number == -1:
            number = offset + len(ids)
    if number is not None:
        index = number - offset
        return Reference(ids[index - 1]) if 1 <= index <= len(ids) else Reference(out_of_range=True)
    if context.get("selectedAppointmentId"):
        return Reference(context["selectedAppointmentId"])
    if len(ids) == 1:
        return Reference(ids[0])
    return Reference(ambiguous=True)


def resolve_listing(question: str, scope: list[str]) -> tuple[str | None, bool, bool]:
    """(listingId, out_of_range, ambiguous) from the last listing scope the application stored."""
    if not scope:
        return None, False, False
    folded = fold(question)
    number: int | None = None
    digit = LISTING_REF.search(folded)
    word = LISTING_WORD_REF.search(folded)
    if digit:
        number = int(digit.group(1))
    elif word:
        number = _WORDS[word.group(1)]
        number = len(scope) if number == -1 else number
    if number is not None:
        return (scope[number - 1], False, False) if 1 <= number <= len(scope) else (None, True, False)
    if len(scope) == 1:
        return scope[0], False, False
    return None, False, True


# ---------------------------------------------------------------- question -> filters (rules)

PART_OF_DAY = [
    (re.compile(r"\bsang\b"), ("00:00", "11:59")),
    (re.compile(r"\bchieu\b"), ("12:00", "17:59")),
    (re.compile(r"\btoi nay\b|\bbuoi toi\b|\btoi mai\b|\bban dem\b"), ("18:00", "23:59")),
]
WEEKDAY_WORDS = [
    ("MONDAY", r"\bthu (?:hai|2)\b"), ("TUESDAY", r"\bthu (?:ba|3)\b"), ("WEDNESDAY", r"\bthu (?:tu|4)\b"),
    ("THURSDAY", r"\bthu (?:nam|5)\b"), ("FRIDAY", r"\bthu (?:sau|6)\b"), ("SATURDAY", r"\bthu (?:bay|7)\b"),
    ("SUNDAY", r"\bchu nhat\b|\bcn\b"),
]
DATE_TEXT = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?(?!\d)")


def part_of_day(folded: str) -> tuple[str, str] | None:
    for pattern, window in PART_OF_DAY:
        if pattern.search(folded):
            return window
    return None


def clock_hour(folded: str) -> tuple[int, int] | None:
    match = re.search(r"\b(\d{1,2})\s*(?:h|gio)\s*(\d{2})?\b", folded)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if "chieu" in folded and hour < 12:
        hour += 12
    elif "toi" in folded and hour < 12 and re.search(r"\btoi nay\b|\bbuoi toi\b|\btoi mai\b", folded):
        hour += 12
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def explicit_date(folded: str, today: date) -> date | None:
    """dd/mm or dd/mm/yyyy. Without a year: the next occurrence on or after today. Invalid dates are ignored."""
    match = DATE_TEXT.search(folded)
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    try:
        if match.group(3):
            return date(int(match.group(3)), month, day)
        candidate = date(today.year, month, day)
        return candidate if candidate >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def date_preset(folded: str) -> str | None:
    if re.search(r"\bhom nay\b|\bbuoi toi nay\b|\btoi nay\b|\bsang nay\b|\bchieu nay\b", folded):
        return "TODAY"
    if re.search(r"\bngay mai\b|\bsang mai\b|\bchieu mai\b|\btoi mai\b|\bmai\b", folded):
        return "TOMORROW"
    if re.search(r"\btuan sau\b|\btuan toi\b", folded):
        return "NEXT_WEEK"
    if re.search(r"\bcuoi tuan\b", folded):
        return "THIS_WEEKEND"
    if re.search(r"\btuan nay\b", folded):
        return "THIS_WEEK"
    if re.search(r"\bthang nay\b", folded):
        return "THIS_MONTH"
    return None


_LISTING_ORDINAL_TEXT = re.compile(r"\b(?:phong|can ho|can|nha|tin)\s+(?:thu\s+|so\s+)?(?:\d{1,2}|" + _WORD_PATTERN + r")\b")


def strip_listing_ordinals(folded: str) -> str:
    """"căn thứ hai" is a list position, not Monday. Removed before weekday detection."""
    return _LISTING_ORDINAL_TEXT.sub(" ", folded)
