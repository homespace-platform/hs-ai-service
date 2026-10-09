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
            return self._unavailable(request_id)

        # Keep listing references resolvable even if the model omits links.
        links = "\n".join(f"- [{item.get('title') or 'Xem tin'}](/rent/{item['id']})" for item in evidence["listings"])
        if evidence.get("relaxedFilters"):
            raw = ("Chưa có tin khớp toàn bộ điều kiện. Mình đã nới tiêu chí "
                   + ", ".join(evidence["relaxedFilters"]) + " để gợi ý các tin liên quan:\n" + raw)
        return AskResult(answer=raw.strip() + "\n\nTin đăng: \n" + links,
                         status="ANSWERED", citations=[], request_id=request_id)

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
