"""Answer questions about live listing fields without letting an LLM invent values.

The model may identify which public fields a question refers to. The final
values, links, comparisons and fees are rendered solely from the MCP result.
"""

import json
import re
import unicodedata
from typing import Any

from homespace_ai.clients.generative import BaseGenerativeClient, GenerationUnavailableError


def _plain(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    return "".join(c for c in value if unicodedata.category(c) != "Mn")


FIELD_LABELS: dict[str, str] = {
    "listing.title": "Tên tin", "listing.description": "Mô tả", "listing.category": "Loại hình",
    "listing.available_from": "Ngày có thể vào", "listing.area_m2": "Diện tích",
    "listing.price_amount": "Giá thuê", "listing.price_unit": "Đơn vị giá",
    "listing.currency": "Đơn vị tiền",
    "listing.negotiable": "Thương lượng", "listing.deposit_type": "Hình thức đặt cọc",
    "listing.deposit_amount": "Tiền cọc", "listing.deposit_months": "Số tháng đặt cọc",
    "listing.payment_cycle": "Chu kỳ thanh toán", "listing.minimum_lease_months": "Thời hạn thuê tối thiểu",
    "listing.management_fee_included": "Phí quản lý đã gồm trong giá",
    "listing.vat_included": "VAT đã gồm trong giá", "listing.max_motorbike_count": "Số xe máy tối đa",
    "listing.max_car_count": "Số ô tô tối đa",
    "address.province_name": "Tỉnh/thành", "address.ward_name": "Phường/xã",
    "address.street_line": "Số nhà và đường", "address.full_address": "Địa chỉ",
    "owner.display_name": "Người đăng tin", "owner.phone": "Số điện thoại liên hệ",
    "room.room_code": "Mã phòng", "room.floor_number": "Tầng phòng",
    "room.restroom_type": "Nhà vệ sinh", "room.kitchen_type": "Khu bếp",
    "room.has_window": "Cửa sổ", "room.balcony_type": "Ban công",
    "room.has_balcony": "Có ban công",
    "room.has_mezzanine": "Gác lửng", "room.furnishing_status": "Nội thất phòng",
    "room.access_type": "Lối đi", "room.access_hours_type": "Giờ giấc sinh hoạt",
    "room.electric_meter_type": "Đồng hồ điện", "room.water_meter_type": "Đồng hồ nước",
    "room.max_occupants": "Số người tối đa", "room.max_vehicles": "Số xe tối đa",
    "room.parking_policy": "Chính sách gửi xe",
    "apartment.project_name": "Dự án/chung cư", "apartment.building_block": "Tòa/Block",
    "apartment.unit_code": "Mã căn hộ", "apartment.floor_number": "Tầng căn hộ",
    "apartment.building_total_floors": "Tổng số tầng tòa nhà",
    "apartment.bedroom_count": "Số phòng ngủ", "apartment.bathroom_count": "Số phòng vệ sinh",
    "apartment.living_room_count": "Số phòng khách", "apartment.kitchen_count": "Số phòng bếp",
    "apartment.furnishing_status": "Tình trạng nội thất",
    "apartment.main_door_direction": "Hướng cửa chính",
    "apartment.balcony_direction": "Hướng ban công", "apartment.view_description": "Tầm nhìn",
    "apartment.max_occupants": "Số người tối đa", "apartment.legal_status": "Giấy tờ pháp lý",
    "house.land_area_m2": "Diện tích đất", "house.frontage_width_m": "Mặt tiền",
    "house.length_m": "Chiều dài", "house.access_road_width_m": "Độ rộng đường/hẻm",
    "house.frontage_count": "Số mặt tiền", "house.total_floors": "Tổng số tầng",
    "house.bedroom_count": "Số phòng ngủ", "house.bathroom_count": "Số phòng tắm/WC",
    "house.living_room_count": "Số phòng khách", "house.kitchen_count": "Số phòng bếp",
    "house.has_rooftop": "Sân thượng", "house.has_garage": "Gara để xe",
    "house.access_type": "Lối đi", "house.max_occupants": "Số người tối đa",
    "house.max_vehicles": "Số xe tối đa", "house.furnishing_status": "Tình trạng nội thất",
    "house.legal_status": "Pháp lý", "house.rental_scope_description": "Phạm vi cho thuê",
    "house.rented_floor_from": "Tầng cho thuê từ", "house.rented_floor_to": "Tầng cho thuê đến",
    "charges": "Các khoản phí hàng tháng",
    "charges.ELECTRICITY": "Tiền điện", "charges.WATER": "Tiền nước",
    "charges.MANAGEMENT": "Phí quản lý", "charges.INTERNET": "Internet/WiFi",
    "charges.SERVICE_OR_GARBAGE": "Phí dịch vụ và rác",
    "charges.MOTORBIKE_PARKING": "Phí gửi xe máy", "charges.CAR_PARKING": "Phí gửi ô tô",
    "amenities": "Tiện ích", "custom_amenities": "Tiện ích khác",
    "furnishings": "Nội thất bàn giao", "viewing_days": "Ngày xem nhà",
    "viewing_slots": "Khung giờ xem nhà", "media": "Ảnh/video",
}


_ALIASES: dict[str, tuple[str, ...]] = {
    "listing.price_amount": ("gia thue", "gia phong", "tien phong", "gia bao nhieu", "re hon", "gia cua"),
    "listing.area_m2": ("dien tich", "bao nhieu m2", "rong bao nhieu"),
    "listing.available_from": ("ngay vao", "chuyen vao", "co san tu ngay"),
    "listing.deposit_type": ("dat coc", "tien coc", "coc bao nhieu"),
    "listing.deposit_amount": ("dat coc", "tien coc", "coc bao nhieu"),
    "listing.deposit_months": ("dat coc", "tien coc", "coc bao nhieu"),
    "listing.payment_cycle": ("thanh toan", "dong tien", "chu ky"),
    "listing.minimum_lease_months": ("thue toi thieu", "hop dong toi thieu"),
    "listing.negotiable": ("thuong luong", "mac ca"),
    "listing.vat_included": ("vat", "thue vat"),
    "address.full_address": ("dia chi", "o dau", "duong nao", "so nha"),
    "owner.display_name": ("chu nha", "nguoi dang", "ai dang tin", "ten nguoi dang"),
    "owner.phone": ("so dien thoai", "sdt", "lien he chu nha", "so lien he"),
    "room.restroom_type": ("wc", "nha ve sinh", "ve sinh rieng", "ve sinh chung"),
    "apartment.bathroom_count": ("wc", "nha ve sinh", "phong tam"),
    "house.bathroom_count": ("wc", "nha ve sinh", "phong tam"),
    "room.kitchen_type": ("khu bep", "bep rieng", "bep chung"),
    "apartment.kitchen_count": ("phong bep", "may bep"),
    "house.kitchen_count": ("phong bep", "may bep"),
    "room.has_window": ("cua so",),
    "room.balcony_type": ("ban cong", "san phoi"),
    "room.has_balcony": ("ban cong",),
    "apartment.balcony_direction": ("huong ban cong",),
    "room.has_mezzanine": ("gac", "gac lung"),
    "room.floor_number": ("tang may", "tang nao", "tang phong"),
    "apartment.floor_number": ("tang may", "tang nao", "tang can ho"),
    "house.total_floors": ("may tang", "bao nhieu tang"),
    "apartment.bedroom_count": ("phong ngu", "may pn"),
    "house.bedroom_count": ("phong ngu", "may pn"),
    "room.max_occupants": ("toi da may nguoi", "so nguoi toi da", "o duoc may nguoi"),
    "apartment.max_occupants": ("toi da may nguoi", "so nguoi toi da", "o duoc may nguoi"),
    "house.max_occupants": ("toi da may nguoi", "so nguoi toi da", "o duoc may nguoi"),
    "room.max_vehicles": ("toi da may xe", "so xe toi da"),
    "house.max_vehicles": ("toi da may xe", "so xe toi da"),
    "room.parking_policy": ("gui xe", "de xe", "do xe", "xe mien phi", "phi gui xe"),
    "house.has_garage": ("gara", "garage", "cho de xe"),
    "charges.MOTORBIKE_PARKING": ("phi gui xe", "gui xe mien phi", "gui xe co phi", "phi de xe", "phi xe may"),
    "charges.CAR_PARKING": ("phi gui o to", "phi do o to", "phi xe hoi"),
    "charges.ELECTRICITY": ("tien dien", "gia dien", "phi dien"),
    "charges.WATER": ("tien nuoc", "gia nuoc", "phi nuoc"),
    "charges.MANAGEMENT": ("phi quan ly",),
    "charges.INTERNET": ("internet", "wifi", "mang"),
    "charges.SERVICE_OR_GARBAGE": ("phi dich vu", "phi rac", "tien rac"),
    "charges": ("cac khoan phi", "chi phi hang thang", "phi hang thang", "tong phi"),
    "room.electric_meter_type": ("dong ho dien", "cong to dien"),
    "room.water_meter_type": ("dong ho nuoc",),
    "amenities": ("tien ich", "ho boi", "thang may", "bao ve", "camera", "thu cung",
                  "nuoi cho", "nuoi meo", "cho nuoi", "phong gym"),
    "furnishings": ("noi that ban giao", "trang thiet bi", "giuong", "tu quan ao", "may lanh", "tinh trang ban giao"),
    "viewing_days": ("ngay xem", "lich xem", "thu may xem"),
    "viewing_slots": ("gio xem", "khung gio xem", "buoi nao xem"),
    "media": ("bao nhieu anh", "co video", "anh that"),
    "house.land_area_m2": ("dien tich dat",),
    "house.frontage_width_m": ("mat tien rong", "chieu rong mat tien"),
    "house.access_road_width_m": ("duong rong", "hem rong", "do rong hem"),
    "house.has_rooftop": ("san thuong",),
    "house.rental_scope_description": (
        "cho thue toan bo", "thue toan bo", "cho thue ca nha", "thue ca nha",
        "thue nguyen can", "chi mot so tang", "thue mot so tang", "cho thue tung tang",
        "thue rieng tung tang", "pham vi thue",
    ),
    "house.rented_floor_from": ("chi mot so tang", "thue mot so tang", "thue tang nao"),
    "house.rented_floor_to": ("chi mot so tang", "thue mot so tang", "thue tang nao"),
    "house.legal_status": ("phap ly", "giay to"),
    "apartment.legal_status": ("phap ly", "giay to"),
    "apartment.project_name": ("du an nao", "chung cu nao", "ten du an"),
    "apartment.building_block": ("toa nao", "block nao"),
    "apartment.view_description": ("tam nhin", "view gi"),
}

_ENUM = {
    "ROOM": "phòng trọ", "APARTMENT": "căn hộ", "HOUSE": "nhà nguyên căn",
    "FREE": "miễn phí", "PAID": "có thu phí", "NONE": "không",
    "PRIVATE": "riêng", "SHARED": "dùng chung", "CURFEW": "có giờ đóng cửa",
    "FLEXIBLE": "giờ giấc linh hoạt", "FULLY_FURNISHED": "đầy đủ nội thất",
    "PARTIALLY_FURNISHED": "nội thất cơ bản", "BASIC": "nội thất cơ bản",
    "UNFURNISHED": "không nội thất", "LUXURY": "nội thất cao cấp",
    "BRAND_NEW": "mới", "GOOD": "tốt", "NORMAL": "bình thường", "USED": "đã sử dụng",
    "MONTHLY": "hàng tháng", "MONTH": "đ/tháng", "ROOM_MONTH": "đ/phòng/tháng",
    "PERSON_MONTH": "đ/người/tháng", "MONTH_COUNT": "theo số tháng thuê",
    "FIXED_AMOUNT": "số tiền cố định", "MONDAY": "Thứ Hai", "TUESDAY": "Thứ Ba",
    "WEDNESDAY": "Thứ Tư", "THURSDAY": "Thứ Năm", "FRIDAY": "Thứ Sáu",
    "SATURDAY": "Thứ Bảy", "SUNDAY": "Chủ nhật", "MORNING": "buổi sáng",
    "AFTERNOON": "buổi chiều", "EVENING": "buổi tối",
}


def is_detail_followup(question: str, has_previous: bool, changed_ward: bool = False) -> bool:
    if not has_previous or changed_ward:
        return False
    plain = _plain(question)
    if any(phrase in plain for phrase in (
        "tim them", "tim phong", "tim nha", "chuyen sang", "bo yeu cau", "bo tieu chi",
        "mo rong khu vuc", "them lua chon", "tim o khu", "cho toi them",
        "con tin nao khac", "lua chon khac", "tim tiep", "them tin",
    )):
        return False
    if any(phrase in plain for phrase in (
        "phong nao", "tin nao", "nha nao", "can nao", "hai phong", "hai tin",
        "2 phong", "2 tin", "vua gui", "vua neu", "truoc do", "so sanh",
        "con lai", "luc dau", "phong do", "tin do", "trong do", "bao nhieu",
        "chi chon", "chi muon", "kiem tra lai", "ban vua noi", "phong nay",
        "can ho nay", "nha nay", "tin nay", "no co", "co duoc", "bao gio",
    )):
        return True
    return bool(_deterministic_fields(question)) or "?" in question


def _deterministic_fields(question: str) -> list[str]:
    plain = _plain(question)
    result: list[str] = []
    for path, label in FIELD_LABELS.items():
        aliases = (_plain(label),) + _ALIASES.get(path, ())
        if any((alias in plain if len(alias) >= 4 else
                re.search(rf"\b{re.escape(alias)}\b", plain)) for alias in aliases):
            result.append(path)
    # A general parking question needs capacity and price as well as the policy.
    if any(term in plain for term in ("gui xe", "de xe", "do xe")):
        result.extend(("room.parking_policy", "room.max_vehicles", "house.has_garage",
                       "house.max_vehicles", "listing.max_motorbike_count",
                       "charges.MOTORBIKE_PARKING"))
    if "house.rental_scope_description" in result:
        result.extend(("house.rented_floor_from", "house.rented_floor_to",
                       "house.total_floors"))
    if re.search(r"\bdien\b", plain) and "dien tich" not in plain:
        result.append("charges.ELECTRICITY")
    if re.search(r"\bnuoc\b", plain) and "nuoc nong" not in plain:
        result.append("charges.WATER")
    return list(dict.fromkeys(result))


async def _select_fields(
    question: str, previous_questions: list[str], client: BaseGenerativeClient,
) -> list[str]:
    selected = _deterministic_fields(question)
    if selected:
        return selected
    if "phi" in _plain(question):
        if previous_questions and any(term in _plain(previous_questions[-1]) for term in (
            "gui xe", "de xe", "do xe",
        )):
            return ["room.parking_policy", "room.max_vehicles", "charges.MOTORBIKE_PARKING"]
        return [key for key in FIELD_LABELS if key.startswith("charges.")]
    if previous_questions and any(term in _plain(question) for term in (
        "con lai", "thi sao", "cai do", "phong do", "tin do", "luc dau",
    )):
        inherited = _deterministic_fields(previous_questions[-1])
        if inherited:
            return inherited
    try:
        prompt = json.dumps({
            "question": question, "previousQuestions": previous_questions[-2:],
            "availableFields": FIELD_LABELS,
        }, ensure_ascii=False)
        response = await client.generate_answer(prompt, [], mode="property_field_select")
        data = json.loads(response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        if isinstance(data, dict) and isinstance(data.get("fields"), list):
            return [key for key in data["fields"][:12] if key in FIELD_LABELS]
    except (GenerationUnavailableError, ValueError, TypeError):
        pass
    return []


def _money(value: Any) -> str:
    return f"{int(float(value)):,}".replace(",", ".") + " đ"


def _value(item: dict, path: str) -> Any:
    if path.startswith("charges."):
        charge_type = path.split(".", 1)[1]
        return next((c for c in item.get("charges", []) if c.get("charge_type") == charge_type), None)
    if "." not in path:
        return item.get(path)
    group, key = path.split(".", 1)
    section = item.get(group)
    return section.get(key) if isinstance(section, dict) else None


def _format_charge(value: dict) -> str:
    if value.get("included_in_rent"):
        return "đã bao gồm trong giá thuê"
    if value.get("amount") is None:
        return "chưa ghi số tiền"
    unit = value.get("unit") or {
        "PER_KWH": "kWh", "PER_M3": "m³", "PER_VEHICLE_MONTH": "xe/tháng",
        "PER_PERSON_MONTH": "người/tháng", "PER_MONTH": "tháng",
    }.get(value.get("billing_method"), "")
    return _money(value["amount"]) + (f"/{unit}" if unit else "")


def _format_value(item: dict, path: str) -> str | None:
    value = _value(item, path)
    if value is None:
        return None
    if path.startswith("charges."):
        return _format_charge(value)
    if path == "charges":
        return "; ".join(
            f"{FIELD_LABELS.get('charges.' + c.get('charge_type', ''), c.get('custom_name') or c.get('charge_type', 'Phí'))}: "
            f"{_format_charge(c)}"
            for c in value
        ) if value else "tin chưa ghi khoản phí"
    if path == "amenities":
        return ", ".join(x["name"] for x in value) if value else "tin chưa ghi tiện ích"
    if path == "custom_amenities":
        return ", ".join(value) if value else "tin chưa ghi"
    if path == "furnishings":
        return ", ".join(
            f"{x['asset_name']} ({x['quantity']}, tình trạng: "
            f"{_ENUM.get(x.get('handover_condition'), x.get('handover_condition') or 'chưa ghi')})"
            for x in value
        ) if value else "tin chưa ghi tài sản bàn giao"
    if path in {"viewing_days", "viewing_slots"}:
        return ", ".join(_ENUM.get(x, x) for x in value) if value else "tin chưa ghi"
    if path == "media":
        images = sum(x.get("type") == "IMAGE" for x in value)
        videos = sum(x.get("type") == "VIDEO" for x in value)
        return f"{images} ảnh, {videos} video"
    if isinstance(value, bool):
        return "có" if value else "không"
    if path in {"listing.price_amount", "listing.deposit_amount"}:
        return _money(value)
    if path.endswith(("area_m2",)):
        return f"{value:g} m²" if isinstance(value, (int, float)) else f"{value} m²"
    if isinstance(value, str):
        return _ENUM.get(value, value)
    return str(value)


def _parking_conflict(item: dict) -> bool:
    room = item.get("room")
    if not room:
        return False
    policy = room.get("parking_policy")
    capacity = room.get("max_vehicles") or 0
    fee = _value(item, "charges.MOTORBIKE_PARKING")
    return bool(
        (policy in {"FREE", "PAID"} and capacity <= 0)
        or (policy == "FREE" and fee and not fee.get("included_in_rent")
            and (fee.get("amount") or 0) > 0)
    )


def _parking_text(item: dict) -> str:
    category = item.get("listing", {}).get("category")
    fee = _value(item, "charges.MOTORBIKE_PARKING")
    if category == "ROOM":
        room = item.get("room") or {}
        policy = room.get("parking_policy")
        capacity = room.get("max_vehicles")
        if policy == "NONE":
            return "tin ghi không hỗ trợ gửi xe"
        if policy not in {"FREE", "PAID"}:
            return "tin chưa ghi rõ chính sách gửi xe"
        prefix = "chính sách ghi miễn phí" if policy == "FREE" else "chính sách ghi có thu phí"
        capacity_text = f", tối đa {capacity} xe" if capacity is not None else ""
        if _parking_conflict(item):
            fee_text = f"; bảng phí ghi {_format_value(item, 'charges.MOTORBIKE_PARKING')}" if fee and policy == "FREE" else ""
            return f"{prefix}{capacity_text}{fee_text} — dữ liệu mâu thuẫn, cần xác nhận với chủ nhà"
        if policy == "PAID":
            fee_text = _format_value(item, "charges.MOTORBIKE_PARKING")
            return f"gửi xe có thu phí{capacity_text}; mức phí {fee_text or 'chưa được ghi'}"
        return f"gửi xe miễn phí{capacity_text}"
    if category == "HOUSE":
        house = item.get("house") or {}
        return (f"có gara, tối đa {house.get('max_vehicles')} xe" if house.get("has_garage")
                else "tin không ghi có gara")
    count = item.get("listing", {}).get("max_motorbike_count")
    fee_text = _format_value(item, "charges.MOTORBIKE_PARKING")
    return f"tối đa {count} xe máy; phí {fee_text or 'chưa được ghi'}" if count is not None else "tin chưa ghi số xe"


def _is_verified_free(item: dict) -> bool:
    room = item.get("room") or {}
    return bool(room.get("parking_policy") == "FREE" and (room.get("max_vehicles") or 0) > 0
                and not _parking_conflict(item))


def _referenced_listings(question: str, listings: list[dict]) -> list[dict]:
    """Narrow explicit references, without guessing which unnamed listing is meant."""
    plain = _plain(question)
    for index, phrases in enumerate((
        ("tin dau", "tin thu nhat", "phong dau", "phong thu nhat", "phong so 1"),
        ("tin thu hai", "tin thu 2", "phong thu hai", "phong thu 2", "phong so 2"),
        ("tin thu ba", "tin thu 3", "phong thu ba", "phong thu 3", "phong so 3"),
    )):
        if any(phrase in plain for phrase in phrases):
            return listings[index:index + 1]
    coded = [item for item in listings if any(
        code and re.search(rf"\b{re.escape(_plain(str(code)))}\b", plain)
        for code in ((item.get("room") or {}).get("room_code"),
                     (item.get("apartment") or {}).get("unit_code"))
    )]
    if coded:
        return coded
    named = [item for item in listings if
             _plain(str((item.get("listing") or {}).get("title") or "")) in plain]
    return named or listings


def _house_scope_text(item: dict) -> str:
    house = item.get("house") or {}
    description = str(house.get("rental_scope_description") or "tin chưa ghi rõ phạm vi cho thuê").rstrip(". ")
    start, end, total = (house.get(key) for key in (
        "rented_floor_from", "rented_floor_to", "total_floors",
    ))
    if start is not None and end is not None:
        description += f"; tầng được cho thuê: {start}–{end}"
        if total is not None:
            description += f" trên tổng {total} tầng"
    return description


async def answer_listing_question(
    *, question: str, facts: dict, previous_questions: list[str],
    generative_client: BaseGenerativeClient,
) -> str:
    listings = facts.get("listings") or []
    if not listings:
        return ("Mình không còn thấy các tin vừa xem trong danh sách đang được đăng, "
                "nên chưa thể xác nhận thông tin của chúng. Bạn muốn mình tìm các tin đang còn hiển thị không?")
    fields = await _select_fields(question, previous_questions, generative_client)
    plain = _plain(question)
    parking = any(path in fields for path in ("room.parking_policy", "charges.MOTORBIKE_PARKING"))
    if "phi" in plain and not parking and previous_questions and any(
        term in _plain(previous_questions[-1]) for term in ("gui xe", "de xe", "do xe")
    ):
        fields = ["room.parking_policy", "room.max_vehicles", "charges.MOTORBIKE_PARKING"]
        parking = True
    if not fields:
        return ("Mình chưa rõ bạn muốn xem thông tin nào của các tin vừa tìm. "
                "Bạn hỏi cụ thể hơn một chút nhé—chẳng hạn giá, diện tích, phí, nội thất, "
                "điều kiện thuê hoặc lịch xem nhà—mình sẽ kiểm tra ngay trên tin đăng.")

    asks_both_parking_types = (
        parking and "mien phi" in plain
        and any(x in plain for x in ("thu phi", "tra phi", "mat phi", "co phi"))
        and any(x in plain for x in ("hai phong", "moi phong", "ca hai", "so sanh", "tung phong"))
        and not any(x in plain for x in ("chi chon", "chi muon", "chi can"))
    )
    free_only = (parking and not asks_both_parking_types and "mien phi" in plain
                 and any(x in plain for x in ("phong nao", "chi chon", "chi muon", "con phong")))
    paid_only = parking and not asks_both_parking_types and "so sanh" not in plain and any(
        x in plain for x in ("phong nao phai tra phi", "phong nao thu phi",
                           "phong co thu phi", "phong phai tra phi")
    )
    selected = _referenced_listings(question, listings)
    if free_only:
        selected = [item for item in selected if _is_verified_free(item)]
    elif paid_only:
        selected = [item for item in selected if (item.get("room") or {}).get("parking_policy") == "PAID"]

    notes: list[str] = []
    if free_only and not selected:
        notes.append("Mình chưa xác nhận được phòng nào gửi xe miễn phí từ dữ liệu hiện tại.")
        selected = [item for item in _referenced_listings(question, listings)
                    if (item.get("room") or {}).get("parking_policy") == "FREE"]
    elif paid_only and not selected:
        return "Mình đã kiểm tra các tin vừa xem: chưa có phòng nào ghi chính sách gửi xe có thu phí."

    scope_fields = {"house.rental_scope_description", "house.rented_floor_from",
                    "house.rented_floor_to", "house.total_floors"}
    if (len(selected) > 1 and "house.rental_scope_description" in fields
            and set(fields).issubset(scope_fields) and all(item.get("house") for item in selected)):
        scopes = {_house_scope_text(item) for item in selected}
        if len(scopes) == 1:
            links = ", ".join(
                f"[{str(item['listing']['title']).replace('[', r'\[').replace(']', r'\]')}](/rent/{item['id']})"
                for item in selected
            )
            return (f"Mình đã kiểm tra lại cả {len(selected)} nhà vừa tìm. "
                    f"Các tin đều ghi: {scopes.pop()}.\n\nXem từng tin: {links}.")

    lines: list[str] = []
    if notes:
        lines.extend(notes)
    elif len(selected) == 1:
        lines.append("Mình đã xem lại thông tin của tin này:")
    else:
        topic = "chỗ gửi xe" if parking else "những thông tin bạn hỏi"
        lines.append(f"Mình đã đối chiếu {len(selected)} tin vừa tìm về {topic}:")
    lines.append("")
    for item in selected:
        title = str(item["listing"]["title"]).replace("[", "\\[").replace("]", "\\]")
        label = f"[{title}](/rent/{item['id']})"
        parts: list[str] = []
        if parking:
            parts.append(_parking_text(item))
        if "house.rental_scope_description" in fields and item.get("house"):
            parts.append(_house_scope_text(item))
        for path in fields:
            if parking and path in {
                "room.parking_policy", "room.max_vehicles", "house.has_garage",
                "house.max_vehicles", "listing.max_motorbike_count", "charges.MOTORBIKE_PARKING",
            }:
                continue
            if path.startswith("room.") and not item.get("room"):
                continue
            if path.startswith("apartment.") and not item.get("apartment"):
                continue
            if path.startswith("house.") and not item.get("house"):
                continue
            if "house.rental_scope_description" in fields and path in {
                "house.rental_scope_description", "house.rented_floor_from",
                "house.rented_floor_to", "house.total_floors",
            }:
                continue
            value = _format_value(item, path)
            if value is not None:
                parts.append(f"{FIELD_LABELS[path].lower()}: {value}")
        if not parts:
            parts.append("tin này chưa ghi các trường bạn hỏi")
        lines.append(f"- {label}: {'; '.join(parts)}.")
    if parking and len(selected) > 1 and not any(_parking_conflict(item) for item in selected):
        free_rooms = [item for item in selected if _is_verified_free(item)]
        paid_rooms = [item for item in selected if (item.get("room") or {}).get("parking_policy") == "PAID"]
        if len(free_rooms) == 1 and paid_rooms:
            free_room = free_rooms[0]
            title = str(free_room["listing"]["title"]).replace("[", "\\[").replace("]", "\\]")
            lines.append(f"\nNếu bạn ưu tiên gửi xe không mất phí, [{title}](/rent/{free_room['id']}) là lựa chọn phù hợp hơn về riêng tiêu chí này.")
    return "\n".join(lines)
