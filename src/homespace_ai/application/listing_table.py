"""Deterministic comparison tables built from full public listing details (never the search preview).

A table answers "what are the facts" so the customer never has to ask for one; the model only advises.
'Chưa rõ' means the listing did not provide the fact; '—' means the listing does not list this item or
the row does not apply to its category.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from homespace_ai.application.vietnamese_text import VietnamesePattern

UNKNOWN = "Chưa rõ"
ABSENT = "—"
COLUMNS_PER_TABLE = 4
LISTING_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SAFE_IMAGE = re.compile(r"^https://[^\s<>()|\\\"']+$")

SECTIONS: dict[str, tuple[str, VietnamesePattern]] = {
    "amenities": ("Tiện ích", VietnamesePattern(
        r"tiện ích(?!\s*(?:xung quanh|lân cận|gần|quanh|bên ngoài|khu vực))|tiện nghi|thang máy|wifi|camera|bảo vệ|an ninh")),
    "furnishings": ("Nội thất", VietnamesePattern(
        r"nội thất|đồ đạc|trang thiết bị|trang bị|bàn giao|máy lạnh|điều hòa|máy giặt|tủ lạnh|giường|tủ quần áo|nóng lạnh")),
    "charges": ("Chi phí", VietnamesePattern(
        r"chi phí|\bphí\b|tiền điện|tiền nước|giá điện|giá nước|điện nước|(?<!đối )\bđiện\b(?! tích)|\bnước\b|internet|"
        r"dịch vụ|\brác\b|gửi xe|để xe|giữ xe|tổng tiền|hằng tháng phải trả")),
    "terms": ("Điều kiện thuê", VietnamesePattern(
        r"\bcọc\b|thanh toán|thời hạn|thuê tối thiểu|tối thiểu bao lâu|hợp đồng|trả trước|thương lượng")),
    "policies": ("Nội quy", VietnamesePattern(
        r"nội quy|chính sách|quy định|thú cưng|vật nuôi|nuôi (?:chó|mèo|thú)|hút thuốc|khách qua đêm|giờ giấc|cho thuê lại|"
        r"báo trước|hoàn cọc|sửa chữa|cải tạo")),
    "viewing": ("Lịch xem", VietnamesePattern(r"lịch xem|xem phòng|xem nhà|giờ xem|ngày xem|đi xem|hẹn xem")),
    "location": ("Vị trí", VietnamesePattern(r"địa chỉ|đường nào|ở đâu|vị trí")),
}
ALL_SECTIONS = VietnamesePattern(
    r"tất cả (?:thông tin|các trường|trường|mục)|toàn bộ (?:thông tin|chi tiết)|đầy đủ (?:thông tin|chi tiết)|"
    r"chi tiết đầy đủ|mọi thông tin|thông tin chi tiết")
TABLE_REQUEST = VietnamesePattern(
    r"so sánh|lập bảng|kẻ bảng|làm bảng|tạo bảng|xuất bảng|bảng so sánh|dạng bảng|bảng (?:tiện ích|nội thất|chi phí|phí|giá)|"
    r"thêm (?:cột|dòng|hàng)|(?:vào|trong|lên) bảng|bảng (?:trên|này|đó)|liệt kê|đối chiếu")
SHOW_REQUEST = VietnamesePattern(
    r"so sánh|bảng|liệt kê|kèm|hiển thị|cho (?:tôi|mình|em) (?:xem|biết)|thông tin|thế nào|ra sao|\bgì\b|bao nhiêu|"
    r"chi tiết|có những|gồm")

UNIT = {"ROOM_MONTH": "phòng/tháng", "PERSON_MONTH": "người/tháng", "MONTH": "tháng"}
SIDE = {"PRIVATE": "Riêng", "SHARED": "Chung", "NONE": "Không có"}
FURNISHING = {"UNFURNISHED": "Chưa có", "BASIC": "Cơ bản", "PARTIALLY_FURNISHED": "Một phần",
              "FULLY_FURNISHED": "Đầy đủ", "LUXURY": "Cao cấp"}
CATEGORY = {"ROOM": "Phòng trọ", "APARTMENT": "Căn hộ", "HOUSE": "Nhà nguyên căn"}
COLUMN = {"ROOM": "Phòng", "APARTMENT": "Căn", "HOUSE": "Nhà"}
CHARGE = {"ELECTRICITY": "Điện", "WATER": "Nước", "MANAGEMENT": "Phí quản lý", "INTERNET": "Internet",
          "SERVICE_OR_GARBAGE": "Dịch vụ/rác", "MOTORBIKE_PARKING": "Gửi xe máy", "CAR_PARKING": "Gửi ô tô",
          "OVERTIME_AIR_CONDITIONING": "Máy lạnh ngoài giờ"}
BILLING = {"FREE": "Miễn phí", "INCLUDED": "Đã gồm trong giá thuê", "NOT_APPLICABLE": "Không áp dụng",
           "NEGOTIABLE": "Thỏa thuận", "STATE_WATER_RATE": "Theo giá nước nhà nước"}
DEPOSIT = {"NONE": "Không cọc", "NEGOTIABLE": "Thỏa thuận"}
CYCLE = {"MONTHLY": "Hằng tháng", "EVERY_2_MONTHS": "2 tháng/lần", "QUARTERLY": "3 tháng/lần",
         "EVERY_6_MONTHS": "6 tháng/lần", "NEGOTIABLE": "Thỏa thuận"}
PERMISSION = {"ALLOWED": "Cho phép", "NOT_ALLOWED": "Không cho phép", "APPROVAL_REQUIRED": "Cần chủ nhà đồng ý"}
DAYS = {"MONDAY": "T2", "TUESDAY": "T3", "WEDNESDAY": "T4", "THURSDAY": "T5", "FRIDAY": "T6",
        "SATURDAY": "T7", "SUNDAY": "CN"}
SLOTS = {"MORNING": "Sáng", "AFTERNOON": "Chiều", "EVENING": "Tối"}

Row = tuple[str, list[str]]


def requested_sections(question: str, *, search_page: bool = False) -> list[str]:
    """Field groups the customer asked to see, in a stable display order."""
    if search_page:
        # A separate "cho mình xem 4 phòng" does not ask for a furniture inventory
        # merely because an earlier sentence says "đã có giường tủ / không quan trọng".
        requested = {name for clause in re.split(r"[.!?;\n]", question) if SHOW_REQUEST.search(clause)
                     for name in requested_sections(clause)}
        return [name for name in SECTIONS if name in requested]
    if ALL_SECTIONS.search(question):
        return list(SECTIONS)
    return [name for name, (_, pattern) in SECTIONS.items() if pattern.search(question)]


def section_labels(sections: list[str]) -> list[str]:
    return [SECTIONS[name][0] for name in sections]


def money(value: Any) -> str | None:
    try:
        return f"{int(Decimal(str(value))):,}".replace(",", ".")
    except (InvalidOperation, TypeError, ValueError):
        return None


def _cell(value: Any, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip().replace("|", "/")
    return text[:limit - 1].rstrip() + "…" if len(text) > limit else text or UNKNOWN


def _count(value: Any, unit: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return UNKNOWN
    return f"{int(value)}{' ' + unit if unit else ''}"


def _yes(value: Any) -> str:
    return "Có" if value is True else "Không" if value is False else UNKNOWN


def _detail(item: dict[str, Any]) -> dict[str, Any]:
    return item.get({"ROOM": "roomDetail", "APARTMENT": "apartmentDetail", "HOUSE": "houseDetail"}.get(item.get("category"), ""), None) or {}


def _price(item: dict[str, Any]) -> str:
    pricing = item.get("pricing") or {}
    amount, unit = money(pricing.get("amount")), UNIT.get(pricing.get("unit"))
    return f"{amount} đ/{unit}" if amount and unit else UNKNOWN


def _area(item: dict[str, Any]) -> str:
    try:
        return f"{Decimal(str(item['areaM2'])).normalize():f} m²"
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return UNKNOWN


def _available(item: dict[str, Any]) -> str:
    value = item.get("availableFrom")
    return f"{value[8:10]}/{value[5:7]}/{value[:4]}" if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value) else UNKNOWN


def _occupants(item: dict[str, Any]) -> str:
    value = _detail(item).get("maxOccupants")
    return f"{value} người" if isinstance(value, int) and not isinstance(value, bool) and value > 0 else UNKNOWN


def _charge(item: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((row for row in item.get("charges") or [] if row.get("chargeType") == kind), None)


def _charge_text(row: dict[str, Any] | None) -> str:
    if row is None:
        return ABSENT
    if row.get("includedInRent") is True or row.get("billingMethod") == "INCLUDED":
        return "Đã gồm trong giá thuê"
    if row.get("billingMethod") in BILLING:
        return BILLING[row["billingMethod"]]
    amount = money(row.get("amount"))
    if amount is None:
        return "Có phí, chưa rõ mức"
    return f"{amount} đ/{row['unit']}" if row.get("unit") else f"{amount} đ"


def _parking(item: dict[str, Any]) -> str:
    if item.get("category") == "ROOM":
        room = _detail(item)
        policy = room.get("parkingPolicy")
        if policy == "NONE":
            return "Không nhận gửi xe"
        if policy not in {"FREE", "PAID"}:
            return UNKNOWN
        count = room.get("maxVehicles")
        prefix = f"Tối đa {count} xe; " if isinstance(count, int) and not isinstance(count, bool) and count > 0 else ""
        if policy == "FREE":
            return prefix + "không thu phí riêng"
        fee = _charge_text(_charge(item, "MOTORBIKE_PARKING"))
        return prefix + ("có phí, chưa rõ mức" if fee == ABSENT else fee)
    fee = _charge(item, "MOTORBIKE_PARKING")
    count = item.get("maxMotorbikeCount")
    if fee is None:
        return f"Tối đa {count} xe" if isinstance(count, int) and not isinstance(count, bool) and count > 0 else UNKNOWN
    prefix = f"Tối đa {count} xe; " if isinstance(count, int) and not isinstance(count, bool) and count > 0 else ""
    return prefix + _charge_text(fee)


def _for(categories: set[str], getter: Callable[[dict[str, Any]], str]) -> Callable[[dict[str, Any]], str]:
    return lambda item: getter(item) if item.get("category") in categories else ABSENT


def _key_rows(listings: list[dict[str, Any]], distances: dict[str, int]) -> list[Row]:
    categories = {item.get("category") for item in listings}
    wards = {(item.get("address") or {}).get("ward") for item in listings}
    getters: list[tuple[str, Callable[[dict[str, Any]], str]]] = []
    if len(categories) > 1:
        getters.append(("Loại hình", lambda item: CATEGORY.get(item.get("category"), UNKNOWN)))
    getters += [("Giá thuê", _price), ("Diện tích", _area)]
    if len(wards) > 1:
        getters.append(("Khu vực", lambda item: (item.get("address") or {}).get("ward") or UNKNOWN))
    if distances:
        getters.append(("Cách địa điểm đã chọn", lambda item: f"{distances[item['id']]:,} m".replace(",", ".")
                         if item["id"] in distances else UNKNOWN))
    getters += [("Có thể nhận", _available), ("Tối đa", _occupants)]
    rooms, homes = {"ROOM"}, {"APARTMENT", "HOUSE"}
    detail = lambda name, labels: lambda item: labels.get(_detail(item).get(name), UNKNOWN)
    if categories & homes:
        getters += [("Phòng ngủ", _for(homes, lambda item: _count(_detail(item).get("bedroomCount")))),
                    ("Phòng tắm/WC", _for(homes, lambda item: _count(_detail(item).get("bathroomCount"))))]
    if "APARTMENT" in categories:
        getters.append(("Tầng", _for({"APARTMENT"}, lambda item: _count(_detail(item).get("floorNumber")) + (
            f"/{_detail(item)['buildingTotalFloors']}" if isinstance(_detail(item).get("buildingTotalFloors"), int)
            and _detail(item).get("floorNumber") is not None else ""))))
    if "HOUSE" in categories:
        getters += [("Số tầng", _for({"HOUSE"}, lambda item: _count(_detail(item).get("totalFloors")))),
                    ("Gara ô tô", _for({"HOUSE"}, lambda item: _yes(_detail(item).get("hasGarage"))))]
    if "ROOM" in categories:
        getters += [("WC", _for(rooms, detail("restroomType", SIDE))), ("Bếp", _for(rooms, detail("kitchenType", SIDE))),
                    ("Cửa sổ", _for(rooms, lambda item: _yes(_detail(item).get("hasWindow")))),
                    ("Ban công", _for(rooms, detail("balconyType", SIDE))),
                    ("Gác lửng", _for(rooms, lambda item: _yes(_detail(item).get("hasMezzanine"))))]
    getters += [("Nội thất", detail("furnishingStatus", FURNISHING)), ("Gửi xe máy", _parking)]
    return [(label, [getter(item) for item in listings]) for label, getter in getters]


def _union(listings: list[dict[str, Any]], name: str, key: Callable[[dict[str, Any]], str | None]) -> list[str]:
    names: list[str] = []
    for item in listings:
        for row in item.get(name) or []:
            value = key(row) if isinstance(row, dict) else str(row)
            if value and value not in names:
                names.append(value)
    return names


def _amenity_rows(listings: list[dict[str, Any]]) -> list[Row]:
    rows: list[Row] = []
    for label in _union(listings, "amenities", lambda row: row.get("name") or row.get("code")):
        rows.append((label, ["Có" if any((row.get("name") or row.get("code")) == label for row in item.get("amenities") or [])
                             else ABSENT for item in listings]))
    for label in _union(listings, "customAmenities", lambda row: None):
        rows.append((_cell(label), ["Có" if label in (item.get("customAmenities") or []) else ABSENT for item in listings]))
    if not rows:
        rows.append(("Tiện ích", [ABSENT for _ in listings]))
    return rows


def _furnishing_rows(listings: list[dict[str, Any]]) -> list[Row]:
    rows: list[Row] = [("Mức nội thất", [FURNISHING.get(_detail(item).get("furnishingStatus"), UNKNOWN) for item in listings])]

    def asset(item: dict[str, Any], label: str) -> str:
        row = next((row for row in item.get("furnishings") or [] if (row.get("assetName") or row.get("itemCode")) == label), None)
        if row is None:
            return ABSENT
        quantity = row.get("quantity")
        parts = [f"{quantity} cái" if isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 0 else None,
                 row.get("handoverConditionLabel")]
        return ", ".join(part for part in parts if part) or "Có"
    rows += [(label, [asset(item, label) for item in listings])
             for label in _union(listings, "furnishings", lambda row: row.get("assetName") or row.get("itemCode"))]
    return rows


def _charge_rows(listings: list[dict[str, Any]]) -> list[Row]:
    rows: list[Row] = []
    kinds = _union(listings, "charges", lambda row: row.get("chargeType"))
    for kind in [k for k in CHARGE if k in kinds] + [k for k in kinds if k not in CHARGE]:
        if kind == "OTHER":
            continue
        rows.append((CHARGE.get(kind, "Phí khác"), [_charge_text(_charge(item, kind)) for item in listings]))
    for label in _union(listings, "charges", lambda row: (row.get("customName") or "Phí khác") if row.get("chargeType") == "OTHER" else None):
        rows.append((_cell(label), [_charge_text(next((row for row in item.get("charges") or [] if row.get("chargeType") == "OTHER"
                                                       and (row.get("customName") or "Phí khác") == label), None)) for item in listings]))
    if not rows:
        rows.append(("Chi phí", [ABSENT for _ in listings]))
    return rows


def _term_rows(listings: list[dict[str, Any]]) -> list[Row]:
    def deposit(item: dict[str, Any]) -> str:
        pricing = item.get("pricing") or {}
        kind = pricing.get("depositType")
        if kind in DEPOSIT:
            return DEPOSIT[kind]
        if kind == "MONTH_COUNT" and isinstance(pricing.get("depositMonths"), (int, float)):
            return f"{int(pricing['depositMonths'])} tháng tiền thuê"
        if kind == "FIXED_AMOUNT" and money(pricing.get("depositAmount")):
            return f"{money(pricing['depositAmount'])} đ"
        return UNKNOWN

    def pricing(name: str, render: Callable[[Any], str]) -> Callable[[dict[str, Any]], str]:
        return lambda item: render((item.get("pricing") or {}).get(name))
    getters = [("Tiền cọc", deposit), ("Kỳ thanh toán", pricing("paymentCycle", lambda v: CYCLE.get(v, UNKNOWN))),
               ("Thuê tối thiểu", pricing("minimumLeaseMonths", lambda v: _count(v, "tháng"))),
               ("Giá thương lượng", pricing("negotiable", _yes)),
               ("Đã gồm phí quản lý", pricing("managementFeeIncluded", _yes)),
               ("Đã gồm VAT", pricing("vatIncluded", _yes))]
    return [(label, [getter(item) for item in listings]) for label, getter in getters]


def _policy_rows(listings: list[dict[str, Any]]) -> list[Row]:
    def value(item: dict[str, Any], code: str) -> str:
        row = next((row for row in item.get("policies") or [] if row.get("code") == code), None)
        if row is None:
            return ABSENT
        raw = row.get("value")
        if raw in PERMISSION:
            return PERMISSION[raw]
        if raw is None or raw == "":
            return UNKNOWN
        return _cell(f"{raw} {row['unitLabel']}" if row.get("unitLabel") else raw, 70)
    labels = {row.get("code"): row.get("label") or row.get("code") for item in listings for row in item.get("policies") or []
              if isinstance(row, dict) and row.get("code")}
    rows = [(_cell(label), [value(item, code) for item in listings]) for code, label in labels.items()]
    return rows or [("Nội quy", [ABSENT for _ in listings])]


def _viewing_rows(listings: list[dict[str, Any]]) -> list[Row]:
    def listed(name: str, labels: dict[str, str]) -> Callable[[dict[str, Any]], str]:
        return lambda item: ", ".join(labels.get(v, v) for v in item.get(name) or [] if v in labels) or UNKNOWN
    return [("Ngày xem", [listed("viewingDays", DAYS)(item) for item in listings]),
            ("Khung giờ xem", [listed("viewingSlots", SLOTS)(item) for item in listings])]


def _location_rows(listings: list[dict[str, Any]]) -> list[Row]:
    return [("Địa chỉ", [_cell((item.get("address") or {}).get("display") or UNKNOWN) for item in listings]),
            ("Phường/xã", [(item.get("address") or {}).get("ward") or UNKNOWN for item in listings])]


BUILDERS: dict[str, Callable[[list[dict[str, Any]]], list[Row]]] = {
    "amenities": _amenity_rows, "furnishings": _furnishing_rows, "charges": _charge_rows, "terms": _term_rows,
    "policies": _policy_rows, "viewing": _viewing_rows, "location": _location_rows,
}


def cover_url(item: dict[str, Any]) -> str | None:
    """Cover image (flagged first, else lowest sortOrder) as a Markdown-safe https URL."""
    images = [m for m in item.get("media") or [] if isinstance(m, dict) and m.get("mediaType") == "IMAGE"]
    images.sort(key=lambda m: (not m.get("cover"), m.get("sortOrder") if isinstance(m.get("sortOrder"), int) else 1 << 30))
    url = next((m.get("url") for m in images if isinstance(m.get("url"), str)), None)
    return url if url and SAFE_IMAGE.match(url) else None


def _label(text: str) -> str:
    return re.sub(r"[\[\]\"\\]", "", re.sub(r"\s+", " ", str(text))).replace("|", "/").strip()[:120]


def _header(item: dict[str, Any], name: str) -> str:
    title = _label(item.get("title") or "")
    return f'[{name}](/rent/{item["id"]}' + (f' "{title}")' if title else ")") if LISTING_ID.match(str(item.get("id"))) else name


def _title(item: dict[str, Any]) -> str:
    return _label(item.get("title") or "") or UNKNOWN


def _image(item: dict[str, Any], name: str) -> str:
    url = cover_url(item)
    if not url:
        return "Chưa có ảnh"
    image = f"![Ảnh {name}](<{url}>)"
    return f"[{image}](/rent/{item['id']})" if LISTING_ID.match(str(item.get("id"))) else image


def listing_table(listings: list[dict[str, Any]], references: list[str], sections: list[str] | None = None,
                  distances: dict[str, int] | None = None) -> str:
    """Markdown tables, one column per listing (5 per table), headed by the selectionScope position.

    The header links to the public listing and the first row is its cover image, so the table replaces cards.
    """
    if not listings:
        return ""
    sections = [name for name in sections or [] if name in BUILDERS]
    if sections:
        rows: list[Row] = [("Giá thuê", [_price(item) for item in listings])]
        for name in sections:
            rows += BUILDERS[name](listings)
    else:
        rows = _key_rows(listings, distances or {})
    categories = {item.get("category") for item in listings}
    noun = COLUMN.get(next(iter(categories)), "Tin") if len(categories) == 1 else "Tin"
    names = [f"{noun} {references.index(item['id']) + 1 if item['id'] in references else index}"
             for index, item in enumerate(listings, 1)]
    headers = [_header(item, name) for item, name in zip(listings, names)]
    images = [_image(item, name) for item, name in zip(listings, names)]
    titles = [_title(item) for item in listings]
    tables = []
    for start in range(0, len(listings), COLUMNS_PER_TABLE):
        part = slice(start, start + COLUMNS_PER_TABLE)
        lines = ["| Tiêu chí | " + " | ".join(headers[part]) + " |",
                 "| --- | " + " | ".join("---" for _ in headers[part]) + " |",
                 "| Tiêu đề | " + " | ".join(titles[part]) + " |",
                 "| Ảnh | " + " | ".join(images[part]) + " |"]
        lines += ["| " + _cell(label) + " | " + " | ".join(_cell(v) for v in values[part]) + " |" for label, values in rows]
        tables.append("\n".join(lines))
    legend = "‘Chưa rõ’: tin đăng chưa cung cấp"
    if any(ABSENT in values for _, values in rows):
        legend += " · ‘—’: tin không ghi mục này hoặc không áp dụng cho loại hình"
    return "\n\n".join(tables) + "\n\n" + legend + "."


def strip_tables(text: str) -> str:
    """Drop model-written Markdown tables when the server already renders the authoritative one."""
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("|")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def strip_images(text: str) -> str:
    """Only server tables may embed listing images; model text never loads remote URLs."""
    return re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
