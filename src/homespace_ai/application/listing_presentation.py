"""Customer-facing labels only; never mutate tool arguments or Core evidence."""

import re


FIELD_LABELS = {
    "category": "loại hình", "price_amount": "mức giá", "price_unit": "đơn vị giá thuê",
    "area_m2": "diện tích", "deposit_type": "hình thức đặt cọc",
    "deposit_amount": "tiền đặt cọc", "deposit_months": "số tháng đặt cọc",
    "payment_cycle": "chu kỳ thanh toán", "minimum_lease_months": "thời hạn thuê tối thiểu",
    "max_motorbike_count": "số xe máy tối đa", "max_car_count": "số ô tô tối đa",
    "negotiable": "thương lượng", "management_fee_included": "phí quản lý trong giá thuê",
    "vat_included": "thuế VAT trong giá thuê", "available_from": "ngày có thể nhận nhà",
    "published_at": "ngày đăng", "expires_at": "ngày hết hạn", "view_count": "lượt xem",
    "province_code": "tỉnh/thành phố", "province_name": "tỉnh/thành phố",
    "ward_code": "phường/xã", "ward_name": "phường/xã", "street_line": "đường",
    "full_address": "địa chỉ", "land_area_m2": "diện tích đất",
    "frontage_width_m": "chiều rộng mặt tiền", "length_m": "chiều dài",
    "access_road_width_m": "độ rộng đường/hẻm", "frontage_count": "số mặt tiền",
    "total_floors": "tổng số tầng", "bedroom_count": "số phòng ngủ",
    "bathroom_count": "số phòng tắm/WC", "living_room_count": "số phòng khách",
    "kitchen_count": "số bếp", "max_occupants": "số người ở tối đa",
    "max_vehicles": "số xe tối đa", "rented_floor_from": "tầng cho thuê bắt đầu",
    "rented_floor_to": "tầng cho thuê kết thúc", "has_rooftop": "sân thượng",
    "has_garage": "gara", "access_type": "lối đi", "furnishing_status": "nội thất",
    "legal_status": "pháp lý", "rental_scope_description": "phạm vi cho thuê",
    "project_name": "tên chung cư", "building_block": "tòa nhà/block",
    "unit_code": "mã căn hộ", "main_door_direction": "hướng cửa chính",
    "balcony_direction": "hướng ban công", "view_description": "tầm nhìn",
    "floor_number": "tầng", "building_total_floors": "tổng số tầng tòa nhà",
    "room_code": "mã phòng", "restroom_type": "nhà vệ sinh", "kitchen_type": "bếp",
    "balcony_type": "ban công", "access_hours_type": "giờ ra vào",
    "electric_meter_type": "đồng hồ điện", "water_meter_type": "đồng hồ nước",
    "parking_policy": "chỗ gửi xe", "has_window": "cửa sổ",
    "has_balcony": "ban công", "has_mezzanine": "gác lửng",
    "included_in_rent": "phí trong giá thuê", "billing_method": "cách tính phí",
}
AMENITY_LABELS = {
    "WIFI": "Wi-Fi", "AIR_CONDITIONER": "máy lạnh", "WATER_HEATER": "máy nước nóng",
    "REFRIGERATOR": "tủ lạnh", "WASHING_MACHINE": "máy giặt", "ELEVATOR": "thang máy",
    "PARKING": "chỗ gửi xe", "SECURITY_24_7": "bảo vệ 24/7", "CAMERA": "camera",
    "PETS_ALLOWED": "cho phép nuôi thú cưng", "SWIMMING_POOL": "hồ bơi", "GYM": "phòng gym",
}
FURNISHING_LABELS = {
    **AMENITY_LABELS, "BED": "giường", "WARDROBE": "tủ quần áo", "WORK_DESK": "bàn làm việc",
    "KITCHEN_SHELF": "kệ bếp", "CURTAIN": "rèm cửa", "FAN": "quạt", "SOFA_SET": "bộ sofa",
    "DINING_SET": "bộ bàn ăn", "TV": "TV", "KITCHEN_CABINET": "tủ bếp",
    "COOKTOP": "bếp nấu", "LIGHTING": "đèn chiếu sáng",
}
CHARGE_LABELS = {
    "ELECTRICITY": "tiền điện", "WATER": "tiền nước", "MANAGEMENT": "phí quản lý",
    "INTERNET": "phí Internet", "SERVICE_OR_GARBAGE": "phí dịch vụ/rác",
    "MOTORBIKE_PARKING": "phí gửi xe máy", "CAR_PARKING": "phí gửi ô tô",
    "OVERTIME_AIR_CONDITIONING": "phí máy lạnh ngoài giờ", "OTHER": "phí khác",
}


def field_label(field: str) -> str:
    parts = field.split(".")
    if parts[0] == "amenity":
        return AMENITY_LABELS.get(parts[-1], "tiện ích")
    if parts[0] == "furnishing":
        asset = FURNISHING_LABELS.get(parts[1], "trang thiết bị") if len(parts) > 1 else "trang thiết bị"
        detail = {"quantity": "số lượng", "handover_condition": "tình trạng bàn giao",
                  "condition_note": "ghi chú bàn giao", "asset_name": "tên"}.get(parts[-1])
        return f"{detail} {asset}" if detail else asset
    if parts[0] == "charge":
        charge = CHARGE_LABELS.get(parts[1], "chi phí") if len(parts) > 1 else "chi phí"
        detail = {"included_in_rent": "đã bao gồm trong giá thuê", "billing_method": "cách tính",
                  "unit": "đơn vị tính", "description": "mô tả", "custom_name": "tên"}.get(parts[-1])
        return f"{charge} {detail}" if detail else charge
    if field == "custom_amenity.name":
        return "tiện ích bổ sung"
    if field.startswith("viewing."):
        return "ngày xem nhà" if parts[-1] == "day_of_week" else "khung giờ xem nhà"
    if field.startswith("media."):
        return "ảnh tin đăng" if parts[-1] == "IMAGE" else "video tin đăng"
    # Unknown future fields must never fall back to exposing implementation names.
    return FIELD_LABELS.get(parts[-1], "một tiêu chí bổ sung")


def relaxed_labels(fields: list[str]) -> str:
    return ", ".join(dict.fromkeys(field_label(field) for field in fields))


def _camel(field: str) -> str:
    head, *tail = field.split("_")
    return head + "".join(word.capitalize() for word in tail)


# Only known technical identifiers: do not globally replace English words, URLs,
# IDs, ordinary numbers, or rewrite facts. Prompt instructions remain primary.
ALIASES = {}
for name, label in FIELD_LABELS.items():
    for alias in (name, _camel(name)):
        if "_" in alias or any(letter.isupper() for letter in alias):
            ALIASES[alias] = label
        for scope in ("house", "apartment", "room", "houseDetail", "apartmentDetail", "roomDetail", "pricing", "address"):
            ALIASES[f"{scope}.{alias}"] = label
for name in AMENITY_LABELS:
    ALIASES[f"amenity.{name}"] = field_label(f"amenity.{name}")
for name in FURNISHING_LABELS:
    for suffix in ("", ".quantity", ".handover_condition", ".condition_note", ".asset_name"):
        key = f"furnishing.{name}{suffix}"
        ALIASES[key] = field_label(key)
for name in CHARGE_LABELS:
    for suffix in ("amount", "included_in_rent", "includedInRent", "billing_method", "unit"):
        key = f"charge.{name}.{suffix}"
        ALIASES[key] = field_label(key.replace("includedInRent", "included_in_rent"))

_KEY = r"(?<![\w/.-])[`\"']?(?P<key>" + "|".join(re.escape(key) for key in sorted(ALIASES, key=len, reverse=True)) + r")[`\"']?(?![\w.-])"
_PAIR = re.compile(_KEY + r"\s*[:=]\s*[`\"']?(?P<value>true|false|null)[`\"']?(?!\w)", re.IGNORECASE)
_FIELD = re.compile(_KEY)
_LOWER_LABELS = {key.lower(): label for key, label in ALIASES.items()}
_ENUM_LABELS = {
    "furnishing_status": {"UNFURNISHED": "chưa có nội thất", "BASIC": "nội thất cơ bản",
                         "PARTIALLY_FURNISHED": "nội thất một phần", "FULLY_FURNISHED": "nội thất đầy đủ", "LUXURY": "nội thất cao cấp"},
    "parking_policy": {"NONE": "không hỗ trợ gửi xe", "FREE": "không thu phí riêng", "PAID": "có thu phí"},
    "restroom_type": {"PRIVATE": "riêng", "SHARED": "chung"},
    "kitchen_type": {"PRIVATE": "riêng", "SHARED": "chung", "NONE": "không có"},
    "balcony_type": {"PRIVATE": "riêng", "SHARED": "chung", "NONE": "không có"},
    "access_type": {"PRIVATE": "riêng", "SHARED": "chung"},
    "access_hours_type": {"FLEXIBLE": "tự do", "CURFEW": "có giờ giới hạn"},
    "electric_meter_type": {"PRIVATE": "riêng", "SHARED": "chung"},
    "water_meter_type": {"PRIVATE": "riêng", "SHARED": "chung"},
    "price_unit": {"MONTH": "tháng", "ROOM_MONTH": "phòng/tháng", "PERSON_MONTH": "người/tháng",
                   "M2_MONTH": "m²/tháng", "SEAT_MONTH": "chỗ/tháng"},
    "billing_method": {"PER_KWH": "theo kWh", "PER_M3": "theo m³", "PER_PERSON_MONTH": "theo người/tháng",
                       "PER_MONTH": "theo tháng", "PER_M2_MONTH": "theo m²/tháng",
                       "PER_VEHICLE_MONTH": "theo xe/tháng", "PER_HOUR": "theo giờ", "FREE": "miễn phí",
                       "INCLUDED": "đã gồm trong giá thuê", "NOT_APPLICABLE": "không áp dụng",
                       "NEGOTIABLE": "thương lượng", "CUSTOM": "theo thỏa thuận",
                       "STATE_WATER_RATE": "theo giá nước nhà nước"},
}
_ENUMS_BY_LABEL = {FIELD_LABELS[name]: values for name, values in _ENUM_LABELS.items()}
_ENUM_PAIR = re.compile(_KEY + r"\s*[:=]\s*[`\"']?(?P<value>[A-Z][A-Z0-9_]+)[`\"']?(?!\w)")


def _customer_fragment(text: str) -> str:
    """Translate explicit known field annotations, preserving missing/false distinction."""
    def pair(match: re.Match) -> str:
        label = _LOWER_LABELS[match["key"].lower()]
        value = match["value"].lower()
        return f"{label}: " + {"true": "Có", "false": "Không", "null": "Chưa rõ"}[value]

    text = _PAIR.sub(pair, text)
    def enum_pair(match: re.Match) -> str:
        label = ALIASES[match["key"]]
        values = _ENUMS_BY_LABEL.get(label)
        if values is None and "billing" in match["key"]:
            values = _ENUM_LABELS["billing_method"]
        value = (values or {}).get(match["value"])
        return f"{label}: {value}" if value else match[0]

    text = _ENUM_PAIR.sub(enum_pair, text)
    # Remove only redundant translated boolean annotations; keep standalone facts
    # and conflicting annotations rather than silently changing their meaning.
    for label in ("sân thượng", "gara", "cửa sổ", "ban công", "gác lửng"):
        for phrase, value in ((f"không có {label}", "Không"), (f"có {label}", "Có")):
            text = re.sub(r"(?<!không )(" + re.escape(phrase) + r")\s*\(\s*" + re.escape(label)
                          + r":\s*" + value + r"\s*\)", r"\1", text, flags=re.IGNORECASE)
    return _FIELD.sub(lambda match: ALIASES[match["key"]], text)


def customer_listing_text(text: str) -> str:
    """Normalize prose only, leaving complete URLs (including query strings) intact."""
    parts = re.split(r"(https?://[^\s<>]+)", text)
    return "".join(part if index % 2 else _customer_fragment(part) for index, part in enumerate(parts))
