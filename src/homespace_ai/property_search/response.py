"""Grounded, conversational replies for live listing searches."""

import re
import unicodedata
from typing import Any

_LISTING_LINK = re.compile(r"/rent/([0-9a-fA-F-]{36})")


def _plain(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def previous_result_ids(
    search_state: dict[str, Any] | None, history: list[dict[str, Any]]
) -> list[str]:
    if search_state is not None:
        if "referenceIds" in search_state:
            return [str(item) for item in search_state["referenceIds"][:20]]
        if "resultIds" in search_state:
            return [str(item) for item in search_state["resultIds"][:20]]
    # Existing conversations predate searchState. Recover only links from the
    # most recent property-search answer, never from arbitrary user messages.
    for message in reversed(history):
        if message.get("role") == "assistant" and message.get("status") == "PROPERTY_SEARCH":
            return list(dict.fromkeys(_LISTING_LINK.findall(str(message.get("content") or ""))))[:20]
    return []


def references_previous_results(question: str, has_previous: bool, changed_ward: bool) -> bool:
    if not has_previous or changed_ward:
        return False
    plain = _plain(question)
    return any(phrase in plain for phrase in (
        "trong khu vuc do", "trong do", "trong so", "cac phong do", "cac tin do",
        "phong do", "phong tren", "tin tren", "vua gui", "vua tim", "truoc do",
        "phong nao", "tin nao", "hai phong", "2 phong", "hai tin", "2 tin",
    ))


def _count(count: int) -> str:
    return {1: "một", 2: "hai", 3: "ba", 4: "bốn", 5: "năm"}.get(count, str(count))


_PRICE_UNITS = {
    "MONTH": "tháng", "ROOM_MONTH": "phòng/tháng",
    "PERSON_MONTH": "người/tháng", "M2_MONTH": "m²/tháng",
    "SEAT_MONTH": "chỗ/tháng",
}


def asks_about_parking(question: str) -> bool:
    plain = _plain(question)
    return any(term in plain for term in ("gui xe", "de xe", "do xe", "bai xe"))


def _parking_label(listing: dict[str, Any]) -> str | None:
    if listing.get("category") == "ROOM":
        if (listing.get("maxVehicles") or 0) <= 0:
            return None
        return {"FREE": "gửi xe miễn phí", "PAID": "gửi xe có thu phí"}.get(
            listing.get("parkingPolicy")
        )
    if listing.get("category") == "HOUSE" and listing.get("hasGarage") and (listing.get("maxVehicles") or 0) > 0:
        return "có chỗ để xe"
    if listing.get("category") == "APARTMENT" and (listing.get("maxMotorbikeCount") or 0) > 0:
        return "có chỗ để xe máy"
    return None


def _parking_conflict(listing: dict[str, Any]) -> bool:
    return (
        listing.get("category") == "ROOM"
        and listing.get("parkingPolicy") in {"FREE", "PAID"}
        and listing.get("maxVehicles") is not None
        and listing["maxVehicles"] <= 0
    )


def _fallback_intro(
    total: int, shown: int, previous_count: int, scoped: bool, parking_question: bool,
    supported_count: int, conflicts: list[dict[str, Any]],
) -> str:
    if scoped and parking_question:
        if not shown:
            return f"Trong {_count(previous_count)} tin mình vừa gửi, chưa có tin nào ghi nhận chỗ gửi xe."
        if supported_count == previous_count and not conflicts:
            intro = f"Trong {_count(previous_count)} tin mình vừa gửi, cả {_count(supported_count)} đều ghi có chỗ gửi xe."
        elif supported_count:
            intro = f"Trong {_count(previous_count)} tin mình vừa gửi, có {_count(supported_count)} tin ghi có chỗ gửi xe."
        else:
            intro = f"Trong {_count(previous_count)} tin mình vừa gửi, chưa có tin nào xác nhận được chỗ gửi xe."
    elif scoped:
        intro = (
            f"Trong {_count(previous_count)} tin vừa xem, có {_count(shown)} tin đáp ứng điều kiện bạn hỏi thêm."
            if shown else
            f"Trong {_count(previous_count)} tin vừa xem, chưa có tin nào đáp ứng điều kiện mới."
        )
    elif shown:
        intro = f"Mình tìm được {_count(total)} tin phù hợp với nhu cầu bạn vừa chia sẻ. Đây là thông tin chính để bạn xem nhanh:"
    else:
        intro = "Mình chưa tìm thấy tin đang đăng nào đáp ứng đầy đủ các điều kiện bạn vừa nêu."
    if len(conflicts) == 1:
        title = str(conflicts[0].get("title") or "một tin")
        intro += (
            f" Riêng tin “{title}” ghi chính sách gửi xe nhưng số xe tối đa là 0; "
            "bạn nên xác nhận lại với chủ nhà."
        )
    elif conflicts:
        intro += (
            f" Tuy nhiên, {_count(len(conflicts))} tin trong số này ghi chính sách "
            "gửi xe nhưng số xe tối đa là 0; bạn nên xác nhận lại với chủ nhà."
        )
    return intro


async def compose_search_reply(
    *,
    question: str,
    result_data: dict[str, Any],
    previous_ids: list[str],
    scoped: bool,
) -> str:
    matches = result_data.get("matches") or []
    total = int(result_data.get("total") or 0)
    parking_question = asks_about_parking(question)
    conflicts = [item for item in matches if _parking_conflict(item)] if parking_question else []
    supported_count = sum(_parking_label(item) is not None for item in matches) if parking_question else 0
    intro = _fallback_intro(
        total, len(matches), len(previous_ids), scoped, parking_question,
        supported_count, conflicts,
    )

    # Even a constrained model previously invented a 3.4M price for a 3.2M
    # listing. Keep every property-search claim deterministic and DB-backed.

    if not matches:
        return intro + (" Bạn có thể nới một tiêu chí, chẳng hạn khu vực hoặc ngân sách, để mình tìm lại nhé."
                        if not scoped else " Bạn có thể nới tiêu chí vừa thêm hoặc mở rộng khu vực để mình tìm tiếp nhé.")

    lines = [intro, ""]
    for listing in matches:
        price = f"{int(listing['price']):,}".replace(",", ".")
        price_unit = _PRICE_UNITS.get(listing.get("priceUnit"), "tháng")
        title = str(listing.get("title") or "Tin cho thuê").replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        detail = _parking_label(listing) if parking_question else None
        suffix = f" · {detail}" if detail else ""
        if parking_question and detail and listing.get("maxVehicles") is not None:
            capacity = int(listing["maxVehicles"])
            suffix += f" · tối đa {capacity} xe"
        elif parking_question and _parking_conflict(listing):
            policy = "thu phí" if listing.get("parkingPolicy") == "PAID" else "miễn phí"
            suffix = f" · chưa xác nhận chỗ gửi xe (chính sách {policy} nhưng tối đa 0 xe)"
        elif parking_question:
            suffix = " · không ghi nhận chỗ gửi xe"
        area = listing.get("areaM2")
        if area is not None:
            suffix += f" · {area:g} m²"
        lines.append(f"- [{title}](/rent/{listing['id']}) — {price} đ/{price_unit}{suffix}")
    if total > len(matches) and not scoped:
        lines.append(f"\nMình đang hiển thị {_count(len(matches))} tin đầu tiên trong số {_count(total)} tin phù hợp.")
    elif not scoped and not parking_question:
        lines.append("\nBạn muốn mình so sánh thêm phí hàng tháng, tiền cọc hoặc tiện ích của những tin này không?")
    return "\n".join(lines)
