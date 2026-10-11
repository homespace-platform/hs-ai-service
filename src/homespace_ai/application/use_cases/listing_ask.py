"""Select RAG or realtime listing tools and ground listing answers in Core API data."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, Request
import structlog

from homespace_ai.application.listing_context import context_for, current_scope, RECOMMENDATION, scoped_selection
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.clients.generative import BaseGenerativeClient, GenerationUnavailableError
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner, ToolPlan
from homespace_ai.core.config import Settings
from homespace_ai.tools.listings import ListingToolError, ListingTools


LISTING_SIGNAL = re.compile(r"(tìm|kiếm|còn|so sánh|phòng|căn hộ|nhà|tin đăng|bài đăng|gửi xe|nội thất|tiện ích)", re.IGNORECASE)
SEARCH_INTENT = re.compile(r"(tìm|kiếm|còn phòng|phòng nào|căn nào|nhà nào|so sánh)", re.IGNORECASE)
SUPPORT_INTENT = re.compile(r"(homespace là|cách đăng tin|hướng dẫn đăng tin|cách đặt lịch|cách thanh toán|điều khoản sử dụng|chính sách homespace|hợp đồng điện tử)", re.IGNORECASE)
logger = structlog.get_logger(__name__)


class ListingAsk:
    def __init__(self, settings: Settings, generative_client: BaseGenerativeClient,
                 gateway: GatewayApiClient, request: Request) -> None:
        self.settings = settings
        self.generative_client = generative_client
        self.tools = ListingTools(gateway, request)
        self.planner = ListingToolPlanner(settings)

    async def try_answer(self, question: str, history: list[dict[str, Any]], request_id: str) -> AskResult | None:
        if SUPPORT_INTENT.search(question) and not SEARCH_INTENT.search(question):
            return None
        scope = current_scope(history)
        selection = scoped_selection(question, history)
        if selection == []:
            return self._clarify(request_id, scope)
        if selection is None and not LISTING_SIGNAL.search(question) and not any(
                LISTING_SIGNAL.search(item.get("content", "")) for item in history[-2:]):
            return None
        stage = "field_catalog"
        try:
            if selection:
                # References already known by the application need neither a field catalog nor a
                # paid planner call. Still read every selected listing from Core, never old facts.
                plan = ToolPlan("get_listing_detail", {"listingId": selection[0]}) if len(selection) == 1 else (
                    ToolPlan("get_listing_details", {"listingIds": selection}))
            else:
                fields = await self.tools.fields()
                stage = "tool_planning"
                plan = await self.planner.plan(question, history, fields)
        except (ListingToolError, GenerationUnavailableError, HTTPException) as error:
            self._log_failure(request_id, stage, error)
            if LISTING_SIGNAL.search(question) or any(LISTING_SIGNAL.search(item.get("content", "")) for item in history[-2:]):
                return self._unavailable(request_id, scope)
            return None
        if plan is None:
            logger.warning("listing_plan_missing", request_id=request_id, scope_count=len(scope))
            if SEARCH_INTENT.search(question) or (scope and LISTING_SIGNAL.search(question)):
                # No function call is not evidence of a Core outage.
                return self._clarify(request_id, scope)
            return None  # Knowledge question: preserve the existing RAG behavior.

        logger.info("listing_tool_selected", request_id=request_id, tool=plan.name,
                    scope_count=len(scope), scoped_followup=bool(selection))
        stage = "tool_execution"
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
                                     status="NO_RESULTS", citations=[], request_id=request_id,
                                     listing_context=context_for([]))
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
        except (ListingToolError, HTTPException, ValueError, TypeError, KeyError) as error:
            self._log_failure(request_id, stage, error, plan.name)
            return self._unavailable(request_id, scope)

        ids = [item["id"] for item in evidence["listings"]]
        is_known_selection = bool(scope) and plan.name != "search_listings" and all(value in scope for value in ids)
        references = scope if is_known_selection else ids
        # Current API facts and stable display positions are separate from past user preferences.
        evidence["selectionScope"] = [{"listingId": value, "position": index}
                                      for index, value in enumerate(references, 1)]
        evidence["recentUserRequests"] = [item["content"] for item in history[-8:]
                                         if item.get("role") == "user"]
        evidence["scopeOnly"] = is_known_selection
        result_context = context_for(references)

        if plan.name == "search_listings" and self._is_browse_query(question):
            return AskResult(answer=self._browse_answer(question, evidence), status="ANSWERED",
                             citations=[], request_id=request_id, listing_context=context_for(ids[:4]),
                             listing_ids=ids[:4])

        if self._is_room_comparison(question, evidence["listings"]):
            return AskResult(answer=self._room_comparison_answer(evidence["listings"], references),
                             status="ANSWERED", citations=[], request_id=request_id,
                             listing_context=result_context, listing_ids=ids)

        # A recommendation mentioning parking must not be reduced to a parking-only template.
        parking_answer = None if RECOMMENDATION.search(question) else self._parking_answer(question, evidence["listings"], references)
        if parking_answer is not None:
            if evidence.get("relaxedFilters"):
                parking_answer = ("Chưa có tin khớp toàn bộ điều kiện. Những tin dưới đây chỉ là gợi ý sau khi bỏ tiêu chí "
                                  + ", ".join(evidence["relaxedFilters"]) + ".\n" + parking_answer)
            return AskResult(answer=parking_answer, status="ANSWERED", citations=[], request_id=request_id,
                             listing_context=result_context, listing_ids=ids)

        try:
            generated = await self.generative_client.generate_listing_answer(question=question, evidence=evidence)
            # Defense in depth: never expose a generated reference outside fresh public tool evidence.
            if any(value not in ids for value in generated.listing_ids):
                raise GenerationUnavailableError("Unknown listing reference.", retryable=False)
            raw = generated.answer
            selected_ids = list(dict.fromkeys(generated.listing_ids))
            # Enforce the no-link contract even if prose ignores the presentation instruction.
            raw = re.sub(r"\[([^\]]+)\]\(/rent/[0-9a-fA-F-]{36}\)", r"\1", raw)
            raw = re.sub(r"(?:https?://[^\s/]+)?/rent/[0-9a-fA-F-]{36}", "", raw)
        except GenerationUnavailableError as error:
            self._log_failure(request_id, "answer_generation", error, plan.name)
            return AskResult(
                answer="Mình đã kiểm tra tin đăng, nhưng chưa thể tạo câu trả lời trọn vẹn lúc này. "
                       "Bạn thử lại hoặc hỏi ít tiêu chí hơn nhé.",
                status="GENERATION_UNAVAILABLE", citations=[], request_id=request_id,
                listing_context=result_context,
            )

        if evidence.get("relaxedFilters"):
            raw = ("Chưa có tin khớp toàn bộ điều kiện. Mình đã nới tiêu chí "
                   + ", ".join(evidence["relaxedFilters"]) + " để gợi ý các tin liên quan:\n" + raw)
        return AskResult(answer=raw.strip(),
                         status="ANSWERED", citations=[], request_id=request_id,
                         listing_context=result_context, listing_ids=selected_ids)

    @staticmethod
    def _clarify(request_id: str, scope: list[str]) -> AskResult:
        answer = (f"Bạn đang muốn chọn trong {len(scope)} tin vừa xem, hay tìm thêm tin mới? "
                  "Nếu hỏi một tin cụ thể, bạn cho mình số thứ tự trong danh sách nhé." if scope else
                  "Bạn cho mình biết loại nhà/phòng và khu vực muốn tìm nhé, để mình kiểm tra đúng nhu cầu của bạn.")
        return AskResult(answer=answer, status="ANSWERED", citations=[], request_id=request_id,
                         listing_context=context_for(scope))

    @staticmethod
    def _log_failure(request_id: str, stage: str, error: Exception, tool: str | None = None) -> None:
        # Do not log prompts, bearer tokens, provider bodies, user history or public listing data.
        logger.warning("listing_request_failed", request_id=request_id, stage=stage,
                       error_type=type(error).__name__, tool=tool,
                       http_status=getattr(error, "status_code", None))

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
                and not RECOMMENDATION.search(question)
                and bool(re.search(r"so sánh", question, re.IGNORECASE))
                and not bool(re.search(
                    r"giá|phí|điện|nước|cọc|diện tích|nội thất|tiện ích|ban công|gác|"
                    r"bếp|wc|vệ sinh|cửa sổ|gửi xe|để xe|người ở|ngày vào|thời hạn",
                    question, re.IGNORECASE)))

    @staticmethod
    def _room_comparison_answer(listings: list[dict[str, Any]], references: list[str] | None = None) -> str:
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

        references = references or [item["id"] for item in listings]
        columns = [f"Phòng {references.index(item['id']) + 1}" for item in listings]
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
        lines.append(f"Bạn xem {len(shown)} lựa chọn bên dưới nhé; bấm vào thẻ sẽ mở chi tiết ở tab mới.")
        lines.append("")
        if total > len(shown):
            lines.append(f"Còn {total - len(shown)} tin khác; mình có thể lọc tiếp nếu bạn cho biết ngân sách hoặc tiện ích ưu tiên.")
        elif len(listings) == 1:
            lines.append("Bạn muốn mình xem kỹ thêm phí, nội thất hay lịch xem của tin này không?")
        else:
            lines.append("Bạn muốn mình lọc tiếp theo ngân sách hoặc tiện ích nào?")
        return "\n".join(lines)

    @staticmethod
    def _unavailable(request_id: str, scope: list[str] | None = None) -> AskResult:
        return AskResult(answer="Mình chưa kiểm tra được tin đăng trực tiếp lúc này, nên không muốn đoán thông tin cho bạn. Bạn thử lại sau ít phút nhé.",
                         status="TOOL_UNAVAILABLE", citations=[], request_id=request_id,
                         listing_context=context_for(scope) if scope else None)

    @staticmethod
    def _parking_answer(question: str, listings: list[dict[str, Any]], references: list[str] | None = None) -> str | None:
        if not re.search(r"(gửi xe|để xe|đỗ xe|gara)", question, re.IGNORECASE):
            return None
        lines = ["Mình vừa kiểm tra thông tin chỗ để xe trong các tin này:"]
        references = references or [item["id"] for item in listings]
        for item in listings:
            name = f"Tin {references.index(item['id']) + 1}"
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
            lines.append(f"- {name}: " + "; ".join(facts) + ".")
        lines.append("Nếu bạn cần, mình có thể lọc tiếp theo mức phí hoặc số xe bạn muốn gửi nhé.")
        return "\n".join(lines)
