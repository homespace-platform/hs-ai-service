"""Select RAG or realtime listing tools and ground listing answers in Core API data."""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, Request

from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.clients.generative import BaseGenerativeClient, GenerationUnavailableError
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner
from homespace_ai.core.config import Settings
from homespace_ai.tools.listings import ListingToolError, ListingTools


LISTING_SIGNAL = re.compile(r"(tìm|kiếm|còn|so sánh|phòng|căn hộ|nhà|tin đăng|bài đăng|gửi xe|nội thất|tiện ích)", re.IGNORECASE)
SEARCH_INTENT = re.compile(r"(tìm|kiếm|còn phòng|phòng nào|căn nào|nhà nào|so sánh)", re.IGNORECASE)
LISTING_ID_IN_HISTORY = re.compile(r"/rent/[0-9a-fA-F-]{36}")
SUPPORT_INTENT = re.compile(r"(homespace là|cách đăng tin|hướng dẫn đăng tin|cách đặt lịch|cách thanh toán|điều khoản sử dụng|chính sách homespace|hợp đồng điện tử)", re.IGNORECASE)


class ListingAsk:
    def __init__(self, settings: Settings, generative_client: BaseGenerativeClient,
                 gateway: GatewayApiClient, request: Request) -> None:
        self.settings = settings
        self.generative_client = generative_client
        self.tools = ListingTools(gateway, request)
        self.planner = ListingToolPlanner(settings)

    async def try_answer(self, question: str, history: list[dict[str, str]], request_id: str) -> AskResult | None:
        if SUPPORT_INTENT.search(question) and not SEARCH_INTENT.search(question):
            return None
        if not LISTING_SIGNAL.search(question) and not any(
                LISTING_SIGNAL.search(item.get("content", "")) for item in history[-2:]):
            return None
        try:
            fields = await self.tools.fields()
            plan = await self.planner.plan(question, history, fields)
        except (ListingToolError, GenerationUnavailableError, HTTPException):
            if LISTING_SIGNAL.search(question) or any(LISTING_SIGNAL.search(item.get("content", "")) for item in history[-2:]):
                return self._unavailable(request_id)
            return None
        if plan is None:
            if SEARCH_INTENT.search(question) or any(
                    LISTING_ID_IN_HISTORY.search(item.get("content", "")) for item in history[-4:]):
                return self._unavailable(request_id)
            return None  # Knowledge question: preserve the existing RAG behavior.

        try:
            if plan.name == "search_listings":
                search = await self.tools.search(plan.arguments)
                ids = search.get("listingIds") or []
                relaxed: list[str] = []
                if not ids:
                    original_filters = plan.arguments.get("filters") or []
                    for index in range(len(original_filters) - 1, -1, -1):
                        field = original_filters[index].get("field", "") if isinstance(original_filters[index], dict) else ""
                        if field in {"category", "province_code", "province_name", "ward_code", "ward_name"}:
                            continue
                        remaining = original_filters[:index] + original_filters[index + 1:]
                        if not remaining:
                            continue
                        candidate = await self.tools.search({**plan.arguments,
                            "filters": remaining})
                        if candidate.get("listingIds"):
                            search = candidate
                            ids = search["listingIds"]
                            relaxed = [field]
                            break
                if not ids:
                    return AskResult(answer="Mình chưa thấy tin đang hiển thị nào khớp đủ các điều kiện bạn vừa nêu. Bạn muốn nới tiêu chí nào (giá, khu vực hay tiện ích) để mình tìm tiếp?",
                                     status="NO_RESULTS", citations=[], request_id=request_id)
                listings = [await self.tools.detail(listing_id) for listing_id in ids[:5]]
                requested_category = next((item.get("value") for item in plan.arguments.get("filters", [])
                                           if isinstance(item, dict) and item.get("field") == "category"
                                           and item.get("op") == "eq"), None)
                if requested_category:
                    listings = [item for item in listings if item.get("category") == requested_category]
                if not listings:
                    raise ListingToolError("Core search and detail disagree on listing category")
                evidence: dict[str, Any] = {"total": search.get("total"), "readAt": search.get("readAt"),
                                             "filters": plan.arguments.get("filters"),
                                             "relaxedFilters": relaxed, "listings": listings}
            elif plan.name == "get_listing_detail":
                listing = await self.tools.detail(plan.arguments["listingId"])
                evidence = {"readAt": "now", "listings": [listing]}
            else:
                ids = plan.arguments["listingIds"]
                if not 2 <= len(ids) <= 5:
                    raise ListingToolError("Comparison requires two to five listing IDs")
                evidence = {"readAt": "now", "listings": [await self.tools.detail(item) for item in ids]}
        except (ListingToolError, HTTPException, ValueError, TypeError):
            return self._unavailable(request_id)

        if plan.name == "search_listings" and self._is_browse_query(question):
            return AskResult(answer=self._browse_answer(question, evidence), status="ANSWERED",
                             citations=[], request_id=request_id)

        if self._is_room_comparison(question, evidence["listings"]):
            return AskResult(answer=self._room_comparison_answer(evidence["listings"]),
                             status="ANSWERED", citations=[], request_id=request_id)

        parking_answer = self._parking_answer(question, evidence["listings"])
        if parking_answer is not None:
            if evidence.get("relaxedFilters"):
                parking_answer = ("Chưa có tin khớp toàn bộ điều kiện. Những tin dưới đây chỉ là gợi ý sau khi bỏ tiêu chí "
                                  + ", ".join(evidence["relaxedFilters"]) + ".\n" + parking_answer)
            return AskResult(answer=parking_answer, status="ANSWERED", citations=[], request_id=request_id)

        try:
            raw = await self.generative_client.generate_answer(
                question=question,
                context_chunks=[{"content": json.dumps(evidence, ensure_ascii=False, default=str)}],
                audience="USER", mode="listing",
            )
        except GenerationUnavailableError:
            return AskResult(
                answer="Mình đã kiểm tra tin đăng, nhưng chưa thể tạo câu trả lời trọn vẹn lúc này. "
                       "Bạn thử lại hoặc hỏi ít tiêu chí hơn nhé.",
                status="GENERATION_UNAVAILABLE", citations=[], request_id=request_id,
            )

        # Keep listing references resolvable even if the model omits links.
        missing_links = [item for item in evidence["listings"] if f"/rent/{item['id']}" not in raw]
        if evidence.get("relaxedFilters"):
            raw = ("Chưa có tin khớp toàn bộ điều kiện. Mình đã nới tiêu chí "
                   + ", ".join(evidence["relaxedFilters"]) + " để gợi ý các tin liên quan:\n" + raw)
        if missing_links:
            raw = raw.strip() + "\n\nXem tin: " + ", ".join(
                f"[{item.get('title') or 'Chi tiết'}](/rent/{item['id']})" for item in missing_links)
        return AskResult(answer=raw.strip(),
                         status="ANSWERED", citations=[], request_id=request_id)

    @staticmethod
    def _is_browse_query(question: str) -> bool:
        return bool(re.match(r"\s*(?:tìm|kiếm|cho (?:tôi|mình) xem)\b", question, re.IGNORECASE)) and not bool(
            re.search(r"so sánh|bao nhiêu|cho (?:tôi|mình) biết|chi tiết|đặt cọc|tiền cọc|\bphí\b|"
                      r"nội thất|tiện ích|điều kiện thuê|lịch xem|tính thế nào", question, re.IGNORECASE))

    @staticmethod
    def _is_room_comparison(question: str, listings: list[dict[str, Any]]) -> bool:
        # A general follow-up comparison has a predictable compact answer. Questions about a
        # particular field still go to the grounded generator so every listing field remains askable.
        return (2 <= len(listings) <= 5
                and all(item.get("category") == "ROOM" for item in listings)
                and bool(re.search(r"so sánh", question, re.IGNORECASE))
                and not bool(re.search(
                    r"giá|phí|điện|nước|cọc|diện tích|nội thất|tiện ích|ban công|gác|"
                    r"bếp|wc|vệ sinh|cửa sổ|gửi xe|để xe|người ở|ngày vào|thời hạn",
                    question, re.IGNORECASE)))

    @staticmethod
    def _room_comparison_answer(listings: list[dict[str, Any]]) -> str:
        def amount(value: Any) -> str:
            try:
                return f"{int(Decimal(str(value))):,}".replace(",", ".")
            except (InvalidOperation, TypeError, ValueError):
                return "Chưa rõ"

        def area(value: Any) -> str:
            try:
                return f"{Decimal(str(value)).normalize():f} m²"
            except (InvalidOperation, TypeError, ValueError):
                return "Chưa rõ"

        def boolean(value: Any) -> str:
            return "Có" if value is True else "Không" if value is False else "Chưa rõ"

        def enum(value: Any, labels: dict[str, str]) -> str:
            return labels.get(value, "Chưa rõ")

        def occupants(item: dict[str, Any]) -> str:
            value = (item.get("roomDetail") or {}).get("maxOccupants")
            return f"{value} người" if isinstance(value, int) and not isinstance(value, bool) and value > 0 else "Chưa rõ"

        def available_from(item: dict[str, Any]) -> str:
            value = item.get("availableFrom")
            if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
                return f"{value[8:10]}/{value[5:7]}/{value[:4]}"
            return "Chưa rõ"

        def parking(item: dict[str, Any]) -> str:
            room = item.get("roomDetail") or {}
            policy = room.get("parkingPolicy")
            if policy == "NONE":
                return "Không hỗ trợ"
            if policy not in {"FREE", "PAID"}:
                return "Chưa rõ"
            count = room.get("maxVehicles")
            prefix = f"Tối đa {count} xe; " if isinstance(count, int) and count > 0 else ""
            if policy == "FREE":
                return prefix + "không thu phí riêng"
            charge = next((entry for entry in item.get("charges") or []
                           if entry.get("chargeType") == "MOTORBIKE_PARKING"), None)
            if charge and charge.get("includedInRent") is True:
                return prefix + "phí đã gồm trong giá"
            if charge and charge.get("amount") is not None:
                unit = charge.get("unit")
                fee = amount(charge["amount"])
                return prefix + (f"{fee} đ/{unit}" if fee != "Chưa rõ" and unit
                                 else "có phí, chưa rõ mức hoặc kỳ tính")
            return prefix + "có phí, chưa rõ mức"

        columns = [f"[Phòng {index}](/rent/{item['id']})"
                   for index, item in enumerate(listings, 1)]
        rows: list[tuple[str, list[str]]] = []
        unit_labels = {"ROOM_MONTH": "phòng/tháng", "PERSON_MONTH": "người/tháng", "MONTH": "tháng"}
        prices = []
        for item in listings:
            pricing = item.get("pricing") or {}
            price = amount(pricing.get("amount"))
            unit = unit_labels.get(pricing.get("unit"), "")
            prices.append(f"{price} đ/{unit}" if price != "Chưa rõ" and unit else "Chưa rõ")
        rows.append(("Giá thuê", prices))
        rows.append(("Diện tích", [area(item.get("areaM2")) for item in listings]))
        rows.append(("Có thể nhận", [available_from(item) for item in listings]))
        rows.append(("Tối đa", [occupants(item) for item in listings]))
        rows.append(("WC", [enum((item.get("roomDetail") or {}).get("restroomType"),
                                  {"PRIVATE": "Riêng", "SHARED": "Chung"}) for item in listings]))
        rows.append(("Bếp", [enum((item.get("roomDetail") or {}).get("kitchenType"),
                                    {"PRIVATE": "Riêng", "SHARED": "Chung", "NONE": "Không có"})
                              for item in listings]))
        rows.append(("Cửa sổ", [boolean((item.get("roomDetail") or {}).get("hasWindow"))
                                 for item in listings]))
        rows.append(("Ban công", [enum((item.get("roomDetail") or {}).get("balconyType"),
                                        {"PRIVATE": "Riêng", "SHARED": "Chung", "NONE": "Không có"})
                                   for item in listings]))
        rows.append(("Gác lửng", [boolean((item.get("roomDetail") or {}).get("hasMezzanine"))
                                   for item in listings]))
        rows.append(("Nội thất", [enum((item.get("roomDetail") or {}).get("furnishingStatus"),
                                        {"UNFURNISHED": "Chưa có", "BASIC": "Cơ bản",
                                         "PARTIALLY_FURNISHED": "Một phần", "FULLY_FURNISHED": "Đầy đủ",
                                         "LUXURY": "Cao cấp"}) for item in listings]))
        rows.append(("Gửi xe máy", [parking(item) for item in listings]))

        lines = [f"Mình đặt {len(listings)} phòng cạnh nhau để bạn dễ xem điểm khác biệt. "
                 "Ô ‘Chưa rõ’ là thông tin tin đăng chưa cung cấp.", "",
                 "| Tiêu chí | " + " | ".join(columns) + " |",
                 "| --- | " + " | ".join("---" for _ in listings) + " |"]
        lines.extend("| " + label + " | " + " | ".join(values) + " |" for label, values in rows)
        lines.append("")
        lines.extend(f"- Phòng {index}: {item.get('title') or 'Tin đăng'}"
                     for index, item in enumerate(listings, 1))
        return "\n".join(lines)

    @staticmethod
    def _browse_answer(question: str, evidence: dict[str, Any]) -> str:
        listings = evidence["listings"]
        first = listings[0]
        category_name = {"ROOM": "phòng trọ", "HOUSE": "nhà nguyên căn", "APARTMENT": "căn hộ"}.get(
            first.get("category"), "tin đăng")
        wards = {item.get("address", {}).get("wardName") for item in listings}
        ward = next(iter(wards)) if len(wards) == 1 else None
        total = evidence.get("total") or len(listings)
        if evidence.get("relaxedFilters"):
            labels = {"price_amount": "mức giá", "room.has_balcony": "ban công",
                      "room.parking_policy": "chỗ gửi xe", "house.has_garage": "gara"}
            relaxed = ", ".join(labels.get(field, field.replace("_", " "))
                                for field in evidence["relaxedFilters"])
            intro = f"Mình chưa thấy tin khớp đủ mọi tiêu chí. Sau khi nới điều kiện {relaxed}, có lựa chọn gần với yêu cầu của bạn:"
        else:
            intro = f"Mình tìm thấy {total} {category_name} đang hiển thị" + (f" tại {ward}" if ward else "") + ":"
        lines = [intro]
        shown = listings[:4]
        for item in shown:
            pricing = item.get("pricing") or {}
            facts = []
            if pricing.get("amount") is not None:
                try:
                    amount = f"{int(Decimal(str(pricing['amount']))):,}".replace(",", ".")
                    unit = {"PERSON_MONTH": "người/tháng", "ROOM_MONTH": "phòng/tháng"}.get(
                        pricing.get("unit"), "tháng")
                    facts.append(f"{amount} đ/{unit}")
                except (InvalidOperation, ValueError):
                    pass
            if item.get("areaM2") is not None:
                try:
                    facts.append(f"{Decimal(str(item['areaM2'])).normalize():f} m²")
                except (InvalidOperation, ValueError):
                    pass
            room = item.get("roomDetail") or {}
            if "ban công" in question.lower() and room.get("balconyType") in {"PRIVATE", "SHARED"}:
                facts.append("ban công riêng" if room["balconyType"] == "PRIVATE" else "ban công chung")
            if re.search(r"gửi xe|để xe", question, re.IGNORECASE):
                policy = room.get("parkingPolicy")
                if policy == "FREE":
                    facts.append("gửi xe máy không thu phí riêng")
                elif policy == "PAID":
                    facts.append("có chỗ gửi xe máy (thu phí)")
            suffix = " — " + " · ".join(facts) if facts else ""
            lines.append(f"- [{item.get('title') or 'Xem tin'}](/rent/{item['id']}){suffix}")
        lines.append("")
        if total > len(shown):
            lines.append(f"Còn {total - len(shown)} tin khác; mình có thể lọc tiếp nếu bạn cho biết ngân sách hoặc tiện ích ưu tiên.")
        elif len(listings) == 1:
            lines.append("Bạn muốn mình xem kỹ thêm phí, nội thất hay lịch xem của tin này không?")
        else:
            lines.append("Bạn muốn mình lọc tiếp theo ngân sách hoặc tiện ích nào?")
        return "\n".join(lines)

    @staticmethod
    def _unavailable(request_id: str) -> AskResult:
        return AskResult(answer="Mình chưa kiểm tra được tin đăng trực tiếp lúc này, nên không muốn đoán thông tin cho bạn. Bạn thử lại sau ít phút nhé.",
                         status="TOOL_UNAVAILABLE", citations=[], request_id=request_id)

    @staticmethod
    def _parking_answer(question: str, listings: list[dict[str, Any]]) -> str | None:
        if not re.search(r"(gửi xe|để xe|đỗ xe|gara)", question, re.IGNORECASE):
            return None
        lines = ["Mình vừa kiểm tra thông tin chỗ để xe trong các tin này:"]
        for item in listings:
            name = item.get("title") or "Tin đăng"
            listing_id = item["id"]
            room = item.get("roomDetail") or {}
            house = item.get("houseDetail") or {}
            charges = item.get("charges") or []
            motorbike = next((charge for charge in charges if charge.get("chargeType") == "MOTORBIKE_PARKING"), None)
            car = next((charge for charge in charges if charge.get("chargeType") == "CAR_PARKING"), None)
            facts = []
            policy = room.get("parkingPolicy")
            if policy == "NONE":
                facts.append("không hỗ trợ gửi xe máy")
            elif policy == "FREE":
                facts.append("gửi xe máy không thu phí riêng")
            elif policy == "PAID":
                facts.append("gửi xe máy có thu phí")
            if motorbike is not None:
                if motorbike.get("includedInRent") is True:
                    facts.append("phí gửi xe máy đã gồm trong giá thuê")
                elif motorbike.get("amount") is not None:
                    try:
                        amount = f"{int(Decimal(str(motorbike['amount']))):,}".replace(",", ".")
                        facts.append(f"phí xe máy {amount} đ/{motorbike.get('unit') or 'xe/tháng'}")
                    except (InvalidOperation, ValueError):
                        facts.append("tin chưa có mức phí gửi xe máy hợp lệ")
            elif policy is None:
                facts.append("tin chưa khai báo phí gửi xe máy")
            if item.get("maxMotorbikeCount") is not None:
                facts.append(f"tối đa {item['maxMotorbikeCount']} xe máy")
            if house.get("hasGarage") is not None:
                facts.append("có gara" if house["hasGarage"] else "không có gara")
            if item.get("maxCarCount") is not None:
                facts.append(f"tối đa {item['maxCarCount']} ô tô")
            if car is not None:
                if car.get("includedInRent") is True:
                    facts.append("phí gửi ô tô đã gồm trong giá thuê")
                elif car.get("amount") is not None:
                    try:
                        amount = f"{int(Decimal(str(car['amount']))):,}".replace(",", ".")
                        facts.append(f"phí ô tô {amount} đ/{car.get('unit') or 'xe/tháng'}")
                    except (InvalidOperation, ValueError):
                        facts.append("tin chưa có mức phí gửi ô tô hợp lệ")
            lines.append(f"- [{name}](/rent/{listing_id}): " + "; ".join(facts) + ".")
        lines.append("Nếu bạn cần, mình có thể lọc tiếp theo mức phí hoặc số xe bạn muốn gửi nhé.")
        return "\n".join(lines)
