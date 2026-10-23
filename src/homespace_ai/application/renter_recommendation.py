"""Renter nearby-amenity recommendation: deterministic parsing, memory merge and answer text.

The model never invents radius, coordinates, distances or the recommendation cap. The application parses
the customer's words, Core owns amenity evidence, and this module renders only what Core returned.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from homespace_ai.application.vietnamese_text import VietnamesePattern, fold_text

RECOMMENDATION_LIMIT = 3
MAX_CANDIDATES = 10  # mirrors Core's per-request cap
MAX_RADIUS_METERS = 10_000
DEFAULT_RADIUS_METERS = 3_000

PROFILE_PATTERNS: list[tuple[str, VietnamesePattern]] = [
    ("STUDENT", VietnamesePattern(r"sinh viên|học sinh|đi học")),
    ("FAMILY_WITH_CHILDREN", VietnamesePattern(r"gia đình|con nhỏ|trẻ em|có con|em bé|trẻ nhỏ")),
    ("REMOTE_WORKER", VietnamesePattern(r"làm việc tại nhà|làm việc từ xa|remote|làm từ xa|freelance")),
    ("WORKER", VietnamesePattern(r"người đi làm|đi làm|nhân viên|công nhân|văn phòng|dân văn phòng")),
]
PROFILE_LABELS = {"STUDENT": "sinh viên", "WORKER": "người đi làm", "FAMILY_WITH_CHILDREN": "gia đình có trẻ nhỏ",
                  "REMOTE_WORKER": "người làm việc tại nhà"}

# Order matters: longer / more specific phrases first.
PRIORITY_PATTERNS: list[tuple[str, Any]] = [
    ("SHOPPING_MALL", VietnamesePattern(r"trung tâm thương mại|\bTTTM\b|vincom|aeon|big c|go!")),
    ("CONVENIENCE_STORE", VietnamesePattern(r"cửa hàng tiện lợi|circle k|bách hóa|bách hoá|\bminimart\b")),
    ("SUPERMARKET", VietnamesePattern(r"siêu thị|co\.?opmart|winmart")),
    ("KINDERGARTEN", VietnamesePattern(r"mầm non|nhà trẻ|trường mẫu giáo|mẫu giáo")),
    ("UNIVERSITY", VietnamesePattern(r"đại học|cao đẳng|học viện|\bđh\b")),
    ("SCHOOL", VietnamesePattern(r"trường học|trường tiểu học|trường cấp|trường thcs|trường thpt|tiểu học")),
    ("HOSPITAL", VietnamesePattern(r"bệnh viện")),
    ("CLINIC", VietnamesePattern(r"phòng khám|trạm y tế")),
    ("PHARMACY", VietnamesePattern(r"nhà thuốc|hiệu thuốc|tiệm thuốc")),
    # Folded "chợ" equals the very common word "cho": match the accented form only.
    ("MARKET", re.compile(r"\bchợ\b", re.IGNORECASE)),
    ("PARK", VietnamesePattern(r"công viên|không gian xanh")),
    ("BANK", VietnamesePattern(r"ngân hàng|\batm\b")),
    ("FOOD", VietnamesePattern(r"quán ăn|nhà hàng|ăn uống|quán cà phê|cafe|cà phê")),
    ("PUBLIC_TRANSPORT", VietnamesePattern(r"xe buýt|xe bus|bến xe|trạm xe|giao thông công cộng|metro|tàu điện")),
    ("GYM", VietnamesePattern(r"phòng gym|\bgym\b|phòng tập|thể dục thể thao")),
]
CATEGORY_LABELS = {
    "UNIVERSITY": "trường đại học/cao đẳng", "SCHOOL": "trường học", "KINDERGARTEN": "trường mầm non",
    "HOSPITAL": "bệnh viện", "CLINIC": "phòng khám", "PHARMACY": "nhà thuốc", "SUPERMARKET": "siêu thị",
    "SHOPPING_MALL": "trung tâm thương mại", "MARKET": "chợ", "PARK": "công viên", "BANK": "ngân hàng",
    "FOOD": "quán ăn", "CONVENIENCE_STORE": "cửa hàng tiện lợi", "PUBLIC_TRANSPORT": "trạm xe buýt/giao thông công cộng",
    "GYM": "phòng gym",
}

_AMENITY_WORDS = VietnamesePattern(
    r"tiện ích (?:xung quanh|lân cận|quanh|gần)|xung quanh|lân cận|bán kính|trong vòng \d|đi bộ|khoảng cách tới")
_RECOMMEND_WORDS = VietnamesePattern(
    r"nên chọn|gợi ý|đề xuất|phù hợp|\bhợp\b|thuận tiện|tốt nhất|\btop\b|so sánh|ưu tiên|chọn (?:giúp|cho)?|xếp hạng|thích hợp")
_NEAR_POI = VietnamesePattern(
    r"gần (?:trường|đại học|đh|cao đẳng|học viện|bệnh viện|chợ|siêu thị|công viên|ngân hàng|trung tâm thương mại|"
    r"cửa hàng tiện lợi|nhà thuốc|phòng khám|bến xe|xe buýt|phòng gym|mầm non)")
_NEW_SEARCH = VietnamesePattern(r"\b(?:tìm|kiếm|tìm kiếm|cho (?:tôi|mình) xem)\b")

_RADIUS = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(km|kilomet|cay so|met|m)(?![\w²³])")
_STOP_TARGET = re.compile(
    r"\s+(?:bán kính|ban kinh|trong|cách|cach|và|va|nhưng|nhung|với|voi|có|co|giá|gia|dưới|duoi|trên|tren|cho|"
    r"thì|thi|nhé|nhe|không|khong|để|de|ở|o|tầm|tam|khoảng|khoang|mà|ma|nên|nen|phù hợp|phu hop|thuận|thuan|"
    r"gần|gan|đi|di)\b.*$", re.IGNORECASE)
_TARGET_START = re.compile(
    r"(?:(?:truong|dh|dai hoc|cao dang|hoc vien)\s+)+(?:[^\s,.;?!]+)(?:\s+[^\s,.;?!]+){0,7}", re.IGNORECASE)


@dataclass
class NearbyRequest:
    profiles: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    radius_meters: int | None = None
    radius_error: str | None = None
    target: str | None = None
    adds: bool = False
    wants_space: bool = False


def parse_radius(question: str) -> tuple[int | None, str | None]:
    """Explicit distance units only; '5 triệu', '3 người' and '30 m²' are never a radius."""
    found = None
    for match in _RADIUS.finditer(fold_text(question)):
        number = Decimal(match.group(1).replace(",", "."))
        factor = 1000 if match.group(2) in {"km", "kilomet", "cay so"} else 1
        found = number * factor
    if found is None:
        return None, None
    if found <= 0:
        return None, "Bán kính phải lớn hơn 0 m. Bạn cho mình bán kính tìm kiếm khác nhé (ví dụ 500 m hoặc 2 km)."
    if found > MAX_RADIUS_METERS:
        return None, (f"Mình chỉ kiểm tra tiện ích trong bán kính tối đa {MAX_RADIUS_METERS // 1000} km, "
                      "bạn thử một bán kính nhỏ hơn nhé.")
    return int(found), None


def parse_target(question: str) -> str | None:
    folded = fold_text(question)
    match = _TARGET_START.search(folded)
    if not match:
        return None
    text = question[match.start():match.end()] if len(folded) == len(question) else folded[match.start():match.end()]
    text = _STOP_TARGET.sub("", text).strip(" -–,.;")
    words = text.split()
    # A bare institution word ("đại học") is a category, not a named place.
    if len(words) < 2 or fold_text(words[-1]) in {"hoc", "dang", "vien", "truong", "dh"}:
        return None
    return text[:120]


def parse_request(question: str) -> NearbyRequest:
    profiles = [name for name, pattern in PROFILE_PATTERNS if pattern.search(question)]
    # "gia đình" beats a stray "đi làm"; remote work is more specific than generic work.
    if "REMOTE_WORKER" in profiles and "WORKER" in profiles:
        profiles.remove("WORKER")
    priorities = [name for name, pattern in PRIORITY_PATTERNS if pattern.search(question)]
    # Longer phrases may also trip generic ones ("trường mầm non" vs school): keep the specific category.
    if "KINDERGARTEN" in priorities and "SCHOOL" in priorities:
        priorities.remove("SCHOOL")
    if "SHOPPING_MALL" in priorities and "SUPERMARKET" in priorities and not VietnamesePattern(r"siêu thị").search(question):
        priorities.remove("SUPERMARKET")
    radius, error = parse_radius(question)
    return NearbyRequest(profiles=profiles, priorities=priorities, radius_meters=radius, radius_error=error,
                         target=parse_target(question), adds=bool(VietnamesePattern(r"thêm|cả|bổ sung").search(question)),
                         wants_space=bool(VietnamesePattern(r"rộng|diện tích lớn|thoáng|to hơn|lớn hơn").search(question)))


def wants_nearby(question: str, parsed: NearbyRequest, state: dict[str, Any] | None, has_scope: bool = False) -> bool:
    if state and state.get("pending") == "SEARCH" and len(question) <= 120 and not _NEW_SEARCH.search(question):
        return True  # answer to a location clarification raised while searching for the candidates
    has_state = bool(state and state.get("candidateIds"))
    if _AMENITY_WORDS.search(question):
        return True
    if (_NEAR_POI.search(question) or parsed.profiles) and _RECOMMEND_WORDS.search(question):
        return True
    if (has_scope or has_state) and not _NEW_SEARCH.search(question) and (
            _NEAR_POI.search(question) or (parsed.priorities and VietnamesePattern(r"\bgan\b|tien ich").search(question))):
        # Rooms are already on screen: "gần siêu thị và trường đại học" ranks THEM, it is not a new text search.
        return True
    if has_state and not _NEW_SEARCH.search(question):
        if state.get("pending") == "TARGET" and len(question) <= 120:
            return True
        if state.get("pending") and (parsed.profiles or parsed.priorities or parsed.target):
            return True
        if parsed.radius_meters is not None or parsed.radius_error:
            return True
        if (parsed.priorities or parsed.profiles) and _RECOMMEND_WORDS.search(question):
            return True
    return False


_ROLE_WORDS = (r"sinh vien|nguoi di lam|di lam|gia dinh(?: co)?(?: (?:tre|con|em be)(?: nho)?)?|lam viec tai nha|"
               r"lam viec tu xa|remote|nhan vien(?: van phong)?|cong nhan|van phong")
_AMENITY_WORDS_FOLDED = (r"truong|dai hoc|dh|cao dang|hoc vien|benh vien|phong kham|nha thuoc|cho|sieu thi|trung tam thuong mai|"
                         r"cua hang tien loi|cong vien|ngan hang|quan an|nha hang|quan ca phe|cafe|ca phe|xe buyt|ben xe|"
                         r"giao thong cong cong|phong gym|gym|mam non")
_STOP = r"(?:duoi|tren|gia|khoang|toi da|tam|voi|va|co|o|trong|cho|de|nhung|nen|\d)"
_SCRUB = [
    # "phù hợp / dành cho / hợp với sinh viên ..."
    rf"(?:phu hop|danh cho|hop|cho)\s+(?:voi\s+)?(?:{_ROLE_WORDS})\b",
    # "gần siêu thị", "gần Đại học Văn Lang" (named place stops at budget/size words)
    rf"\b(?:gan|canh|sat|quanh)\s+(?:{_AMENITY_WORDS_FOLDED})\b(?:\s+(?!{_STOP}\b)[^\s,.;?!]+){{0,6}}",
    r"(?:trong\s+)?(?:ban kinh|cach)\s*\d+(?:[.,]\d+)?\s*(?:km|kilomet|met|m)\b",
    r"\b\d+(?:[.,]\d+)?\s*(?:km|kilomet)\b",
    r"\b(?:tien ich (?:xung quanh|lan can|quanh)|xung quanh|nen chon|goi y|de xuat|xep hang|phong nao)\b",
]


def search_text(question: str) -> str:
    """Candidate search uses listing criteria only. Role and nearby-amenity wishes belong to the next step,
    so they must not become keyword/landmark filters that make a valid search return nothing."""
    folded = fold_text(question)
    if len(folded) != len(question):
        return question
    spans: list[tuple[int, int]] = []
    for pattern in _SCRUB:
        spans.extend(match.span() for match in re.finditer(pattern, folded))
    text = list(question)
    for start, end in spans:
        for index in range(start, end):
            text[index] = " "
    cleaned = re.sub(r"\s+", " ", "".join(text)).strip(" ,.;")
    cleaned = re.sub(r"(?:,\s*){2,}", ", ", cleaned)
    return cleaned or question


def merge_state(previous: dict[str, Any] | None, parsed: NearbyRequest) -> dict[str, Any]:
    """Update only what the customer said; budget/WC/other listing preferences live elsewhere."""
    state = {"version": 1, "profiles": [], "priorities": [], "radiusMeters": None, "targetPlace": None,
             "candidateIds": [], "pending": None, "pendingQuestion": None, "wantsSpace": False}
    if isinstance(previous, dict):
        for key in state:
            if key in previous:
                state[key] = previous[key]
    if parsed.profiles:
        state["profiles"] = parsed.profiles
    if parsed.priorities:
        state["priorities"] = list(dict.fromkeys((state["priorities"] if parsed.adds else []) + parsed.priorities))
    if parsed.radius_meters is not None:
        state["radiusMeters"] = parsed.radius_meters
    if parsed.target:
        state["targetPlace"] = parsed.target
    if parsed.wants_space:
        state["wantsSpace"] = True
    return state


def _price(item: dict[str, Any]) -> tuple[Decimal | None, str]:
    pricing = item.get("pricing") or {}
    try:
        amount = Decimal(str(pricing.get("amount")))
    except (InvalidOperation, TypeError, ValueError):
        return None, "chưa rõ giá"
    unit = {"ROOM_MONTH": "phòng/tháng", "PERSON_MONTH": "người/tháng", "MONTH": "tháng"}.get(pricing.get("unit"), "")
    text = f"{int(amount):,}".replace(",", ".") + " đ" + (f"/{unit}" if unit else "")
    return amount, text


def _meters(value: Any) -> str:
    try:
        meters = int(value)
    except (TypeError, ValueError):
        return "không rõ khoảng cách"
    return f"{meters} m" if meters < 1000 else f"{meters / 1000:.1f} km".replace(".", ",")


def _radius_text(meters: int) -> str:
    return f"{meters} m" if meters < 1000 else f"{meters / 1000:g} km".replace(".", ",")


def _owner_text(state: dict[str, Any]) -> str:
    profiles = [PROFILE_LABELS[name] for name in state.get("profiles") or [] if name in PROFILE_LABELS]
    priorities = [CATEGORY_LABELS[name] for name in state.get("priorities") or [] if name in CATEGORY_LABELS]
    parts = []
    if profiles:
        parts.append("nhu cầu " + " / ".join(profiles))
    if priorities:
        parts.append("ưu tiên " + ", ".join(priorities))
    if state.get("targetPlace"):
        parts.append(f"gần “{state['targetPlace']}”")
    return "; ".join(parts) or "nhu cầu của bạn"


def clarification(state: dict[str, Any]) -> str:
    return ("Để mình xếp hạng theo tiện ích xung quanh cho đúng, bạn cho mình biết bạn thuê cho ai "
            "(sinh viên, người đi làm, gia đình có trẻ nhỏ hay làm việc tại nhà) "
            "hoặc nơi/tiện ích bạn ưu tiên gần (trường, bệnh viện, siêu thị, chợ...) nhé.")


def target_clarification(target: dict[str, Any]) -> str:
    names = list(dict.fromkeys(option.get("name") for option in target.get("options") or [] if option.get("name")))[:4]
    listing = "\n".join(f"- {name}" for name in names)
    return (f"Mình tìm thấy nhiều địa điểm gần với “{target.get('query')}” nên chưa dám chọn thay bạn:\n{listing}\n"
            "Bạn muốn dùng địa điểm nào (ghi đúng tên đầy đủ hoặc cơ sở) để mình so các phòng theo đó nhé?")


def fallback_answer(status: str, state: dict[str, Any]) -> str | None:
    if status == "GEOAPIFY_NOT_CONFIGURED":
        return ("Hệ thống chưa bật dữ liệu bản đồ tiện ích xung quanh nên mình chưa thể xếp hạng các phòng theo khoảng cách "
                "tới trường, siêu thị hay bệnh viện, và cũng không đoán thay dữ liệu. Bạn vẫn có thể nhờ mình so sánh giá, "
                "diện tích và tiện nghi của các phòng đang xem.")
    if status == "PROVIDER_UNAVAILABLE":
        return ("Mình tạm thời chưa lấy được dữ liệu tiện ích xung quanh từ nhà cung cấp bản đồ, nên chưa thể chọn ra "
                "top phòng theo vị trí lúc này. Bạn thử lại sau ít phút nhé; mình có thể so sánh giá và tiện nghi trong lúc chờ.")
    if status == "BUDGET_EXHAUSTED":
        return ("Lượt kiểm tra này đã chạm giới hạn truy vấn bản đồ trước khi đủ dữ liệu để so sánh công bằng giữa các phòng. "
                "Bạn thử chọn ít phòng hơn hoặc thu hẹp bán kính nhé.")
    if status == "NO_VALID_CANDIDATES":
        return ("Các tin này hiện không thể đánh giá vị trí (tin không còn hiển thị hoặc thiếu tọa độ hợp lệ), "
                "nên mình chưa có phòng nào để gợi ý theo tiện ích xung quanh.")
    return None


_FURNISHING = {"UNFURNISHED": "chưa có nội thất", "BASIC": "nội thất cơ bản", "PARTIALLY_FURNISHED": "một phần nội thất",
               "FULLY_FURNISHED": "đủ nội thất", "LUXURY": "nội thất cao cấp"}
NEAR_TIE_METERS = 100  # below this gap between rooms, distance alone should not decide


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _distance(reason: dict[str, Any]) -> int | None:
    try:
        return int(reason.get("distanceMeters"))
    except (TypeError, ValueError):
        return None


def _facts(item: dict[str, Any]) -> str:
    parts = [_price(item)[1]]
    try:
        parts.append(f"{Decimal(str(item.get('areaM2'))).normalize():f} m²")
    except (InvalidOperation, TypeError, ValueError):
        pass
    furnishing = _FURNISHING.get((item.get("roomDetail") or {}).get("furnishingStatus"))
    if furnishing:
        parts.append(furnishing)
    return " · ".join(parts)


def build_answer(response: dict[str, Any], state: dict[str, Any], details: dict[str, dict[str, Any]],
                 positions: dict[str, int], recommended: list[str]) -> str:
    """Shared area facts once, only differences per room, then an explicit recommendation."""
    radius = int(response.get("radiusMeters") or state.get("radiusMeters") or DEFAULT_RADIUS_METERS)
    ranking = {item["listingId"]: item for item in response.get("ranking") or []}
    candidates = {item["listingId"]: item for item in response.get("candidates") or []}
    status = response.get("status")
    def area_of(value: str) -> Decimal:
        try:
            return Decimal(str((details.get(value) or {}).get("areaM2")))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal(0)

    if state.get("wantsSpace"):  # "phòng rộng": among the amenity-ranked rooms, the larger one is listed first
        recommended = sorted(recommended, key=lambda value: -area_of(value))
    count = len(recommended)
    label_of = lambda value: f"Tin {positions.get(value, 0)}"  # noqa: E731

    # category -> {listing_id: reason}, categories in the order Core ranked them.
    by_room: dict[str, dict[str, dict[str, Any]]] = {}
    categories: list[str] = []
    for listing_id in recommended:
        by_room[listing_id] = {}
        for reason in (ranking.get(listing_id) or {}).get("reasons", []):
            category = str(reason.get("category"))
            by_room[listing_id].setdefault(category, reason)
            if category not in categories:
                categories.append(category)

    lines = [f"Mình đã đối chiếu tiện ích quanh {response.get('comparedCount', 0)} phòng trong bán kính "
             f"{_radius_text(radius)} theo {_owner_text(state)}."]
    target = response.get("target") or {}
    if target.get("status") == "CONFIRMED" and target.get("options"):
        lines.append(f"Mốc bạn chọn: {target['options'][0].get('name')}.")
    elif target.get("status") == "NOT_FOUND":
        lines.append(f"Mình chưa thấy “{target.get('query')}” trong dữ liệu bản đồ nên phần xếp hạng chỉ dựa trên các tiện ích khác.")

    # ---- 1. area facts, each place once -------------------------------------------------------------
    area_lines = []
    differs: dict[str, bool] = {}
    for category in categories:
        entries = [(room, by_room[room][category]) for room in recommended if category in by_room[room]]
        names = list(dict.fromkeys(str(reason.get("poiName") or reason.get("label")) for _, reason in entries))
        distances = [value for _, reason in entries if (value := _distance(reason)) is not None]
        differs[category] = len(names) > 1
        label = str(entries[0][1].get("label") or category)
        spread = f"{_meters(min(distances))}" if distances and min(distances) == max(distances) else (
            f"{min(distances)}–{_meters(max(distances))}" if distances and max(distances) < 1000 else
            (f"{_meters(min(distances))} – {_meters(max(distances))}" if distances else ""))
        shown = names[0] if len(names) == 1 else " / ".join(names)
        shown = shown if shown.lower().startswith(label.lower()) else f"{_cap(label)}: {shown}"
        where = "" if len(entries) == count else f" (chỉ thấy ở {', '.join(label_of(room) for room, _ in entries)})"
        area_lines.append(f"- {shown} — cách {spread}" + (" tùy phòng" if len(set(distances)) > 1 else "") + where)
    if area_lines:
        lines += ["", "**Tiện ích quanh khu vực này:**"] + area_lines
    common_missing = None
    for listing_id in recommended:
        notes = set((ranking.get(listing_id) or {}).get("missing") or [])
        common_missing = notes if common_missing is None else common_missing & notes
    for note in sorted(common_missing or []):
        lines.append(f"- {note} (áp dụng cho cả {count} phòng)")

    # ---- 2. only what differs per room ----------------------------------------------------------------
    lines += ["", "**Khác biệt giữa các phòng:**"]
    for order, listing_id in enumerate(recommended, 1):
        item = details.get(listing_id, {})
        address = (item.get("address") or {}).get("street") or ""
        near = []
        for category in categories:
            reason = by_room[listing_id].get(category)
            if reason is None:
                continue
            name = f" ({reason.get('poiName')})" if differs.get(category) and reason.get("poiName") else ""
            near.append(f"{str(reason.get('label'))} {_meters(reason.get('distanceMeters'))}{name}")
        extra = [note for note in (ranking.get(listing_id) or {}).get("missing") or [] if note not in (common_missing or set())]
        lines.append(f"{order}. **{label_of(listing_id)}** — {_facts(item)}" + (f" · {address}" if address else "")
                     + ("\n   " + "; ".join(near) if near else "") + ("\n   " + "; ".join(extra) if extra else ""))

    # ---- 3. recommendation ---------------------------------------------------------------------------
    top = recommended[0]
    wins, close = [], []
    for category in categories:
        mine = by_room[top].get(category)
        rivals = [value for room in recommended[1:] if (reason := by_room[room].get(category)) and (value := _distance(reason)) is not None]
        own = _distance(mine) if mine else None
        if own is None or not rivals:
            continue
        gap = min(rivals) - own
        if gap > 0:
            wins.append((str(mine.get("label")), gap))
        if abs(gap) < NEAR_TIE_METERS:
            close.append(category)
    lines.append("")
    top_amount, _ = _price(details.get(top, {}))
    best_gap: dict[str, int] = {}
    for label, gap in wins:  # one clause per amenity type, with its largest advantage
        best_gap[label] = max(gap, best_gap.get(label, 0))
    reasons = [f"gần {label} hơn" + (f" (hơn khoảng {_meters(gap)})" if gap >= 20 else "") for label, gap in list(best_gap.items())[:3]]
    if state.get("wantsSpace") and all(area_of(top) > area_of(room) for room in recommended[1:]) and area_of(top) > 0:
        reasons.insert(0, f"rộng nhất nhóm ({area_of(top).normalize():f} m²)")
    if reasons:
        lines.append(f"**Mình đề xuất chọn {label_of(top)}** — {'; '.join(reasons)} so với các phòng còn lại.")
    else:
        lines.append(f"**Mình đề xuất xem {label_of(top)} trước** — tổng thể tiện ích đứng đầu trong nhóm này.")
    trade = []
    for listing_id in recommended[1:]:
        item = details.get(listing_id, {})
        amount, _price_text = _price(item)
        notes = []
        if amount is not None and top_amount is not None and amount < top_amount:
            notes.append(f"rẻ hơn {int(top_amount - amount):,}".replace(",", ".") + " đ/tháng")
        try:
            if Decimal(str(item.get("areaM2"))) >= Decimal(str(details[top].get("areaM2"))) + 3:
                notes.append("rộng hơn")
        except (InvalidOperation, TypeError, ValueError, KeyError):
            pass
        for category in categories:
            mine, other = by_room[top].get(category), by_room[listing_id].get(category)
            if mine and other and (_distance(other) or 0) + NEAR_TIE_METERS <= (_distance(mine) or 0):
                notes.append(f"gần {str(other.get('label')).lower()} hơn")
        if notes:
            trade.append(f"{label_of(listing_id)} {', '.join(dict.fromkeys(notes))}")
    if trade:
        lines.append("Đổi lại: " + "; ".join(trade) + ".")
    if categories and len(close) == len(categories):
        lines.append(f"Lưu ý: khoảng cách giữa các phòng chênh nhau dưới {NEAR_TIE_METERS} m nên tiện ích chưa đủ để loại phòng nào; "
                     "bạn nên quyết định thêm theo giá, diện tích, nội thất và đi xem thực tế.")

    skipped = [(positions.get(key, 0), value.get("status")) for key, value in candidates.items()
               if value.get("status") in {"NOT_AVAILABLE", "MISSING_COORDINATES", "INVALID_COORDINATES"}]
    if skipped:
        lines.append("Chưa đánh giá được vị trí của: " + ", ".join(
            f"Tin {position}" for position, _ in sorted(skipped)) + " (tin không còn hiển thị hoặc thiếu tọa độ hợp lệ).")
    if status == "PARTIAL":
        lines.append("Một số điểm dữ liệu bản đồ chưa kiểm tra đủ (giới hạn truy vấn hoặc dữ liệu chưa đầy đủ) nên thứ hạng mang tính tham khảo.")
    lines.append("Khoảng cách chỉ để tham khảo, không phải thời gian di chuyển thực tế; giá và tình trạng tin được đọc trực tiếp lúc này.")
    sources = response.get("sources") or []
    if sources:
        lines.append("Nguồn dữ liệu địa điểm: " + "; ".join(dict.fromkeys(
            str(source.get("attribution") or source.get("name")) for source in sources)) + ".")
    lines.append("Bạn có thể đổi bán kính hoặc tiện ích ưu tiên, mình sẽ tính lại ngay.")
    return "\n".join(lines).strip()
