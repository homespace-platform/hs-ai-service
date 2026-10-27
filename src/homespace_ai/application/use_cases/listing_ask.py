"""Select RAG or realtime listing tools and ground listing answers in Core API data."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, Request
import structlog

from homespace_ai.application.listing_context import context_for, current_scope, RECOMMENDATION, scoped_selection
from homespace_ai.application.listing_presentation import customer_listing_text, relaxed_labels
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.clients.generative import BaseGenerativeClient, GenerationUnavailableError
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner, ToolPlan
from homespace_ai.core.config import Settings
from homespace_ai.tools.listings import ListingToolError, ListingTools
from homespace_ai.application.vietnamese_text import VietnamesePattern
from homespace_ai.application.listing_location import LocationClarification
from homespace_ai.application.listing_evidence import compact_search_listing
from homespace_ai.application.listing_limits import max_results, requested_count, search_limit, ABSOLUTE_MAX_RESULTS
from homespace_ai.application import budget_stretch as stretch


LISTING_SIGNAL = VietnamesePattern(
    r"\b(tìm|kiếm|còn|so sánh|phòng|căn hộ|chung cư|studio|nhà|trọ|chỗ ở|nơi ở|chỗ thuê|thuê|tin đăng|bài đăng|"
    r"gửi xe|nội thất|tiện ích|ngân sách|triệu|tr/tháng|giá rẻ|rẻ nhất|ở ghép|ở một mình)\b")
SEARCH_INTENT = VietnamesePattern(
    r"\b(tìm|kiếm|còn phòng|phòng nào|căn nào|nhà nào|so sánh|cần thuê|muốn thuê|cần tìm|muốn tìm|"
    r"chỗ ở|nơi ở|chỗ thuê|cần chỗ|kiếm chỗ)\b")
SUPPORT_INTENT = VietnamesePattern(r"(homespace là|cách đăng tin|hướng dẫn đăng tin|cách đặt lịch|cách thanh toán|điều khoản sử dụng|chính sách homespace|hợp đồng điện tử)")
# Exact distances and nearby-place ranking require a map capability which is not available.
# Landmark text searches and "tin mới đăng gần nhất" still use the listing search.
MAP_EVIDENCE_REQUEST = VietnamesePattern(
    r"quét.{0,40}(?:xung quanh|tiện ích)|bán kính|khoảng cách (?:đến|tới)|"
    r"tiện ích (?:xung quanh|lân cận)|"
    r"gần (?:nhất|hơn).{0,40}(?:trường|đại học|siêu thị|bệnh viện|công viên|chợ|gym)|"
    r"(?:phòng|căn|nhà|tin) nào.{0,60}gần.{0,40}(?:trường|đại học|siêu thị|bệnh viện|công viên|chợ|gym)"
    r".{0,40}(?:hơn|nhất)\b")
logger = structlog.get_logger(__name__)


class ListingAsk:
    def __init__(self, settings: Settings, generative_client: BaseGenerativeClient,
                 gateway: GatewayApiClient, request: Request) -> None:
        self.settings = settings
        self.generative_client = generative_client
        self.tools = ListingTools(gateway, request, max_results(settings))
        self.planner = ListingToolPlanner(settings)

    async def try_answer(self, question: str, history: list[dict[str, Any]], request_id: str) -> AskResult | None:
        if SUPPORT_INTENT.search(question) and not SEARCH_INTENT.search(question):
            return None
        if MAP_EVIDENCE_REQUEST.search(question):
            return AskResult(
                answer="Hiện mình chưa có dữ liệu quét tiện ích xung quanh hoặc đo khoảng cách để xếp hạng phòng theo vị trí. "
                       "Mình vẫn có thể giúp tìm và so sánh phòng theo khu vực, giá thuê, diện tích và tiện nghi có trong tin đăng.",
                status="ANSWERED", citations=[], request_id=request_id,
                listing_context=context_for(current_scope(history)))
        extras: dict[str, Any] = {}
        result = await self._listing_flow(question, history, request_id, extras)
        return self._with_stretch(result, extras.get("stretch"))

    async def _listing_flow(self, question: str, history: list[dict[str, Any]], request_id: str,
                            extras: dict[str, Any] | None = None) -> AskResult | None:
        scope = current_scope(history)
        selection = scoped_selection(question, history)
        if selection == []:
            return self._clarify(request_id, scope)
        if selection is None and requested_count(question) is None and not LISTING_SIGNAL.search(question) and not any(
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
        except LocationClarification as error:
            return AskResult(answer=str(error), status='ANSWERED', citations=[], request_id=request_id,
                             listing_context=context_for(scope))
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
                limit = search_limit(question, self.settings)
                plan = ToolPlan(plan.name, {**plan.arguments, 'limit': limit})
                search = await self.tools.search(plan.arguments)
                ids = search.get("listingIds") or []
                relaxed: list[str] = []
                if not ids:
                    original_filters = plan.arguments.get("filters") or []
                    retries = 0
                    for index in range(len(original_filters) - 1, -1, -1):
                        field = original_filters[index].get("field", "") if isinstance(original_filters[index], dict) else ""
                        if field in {"category", "city_id", "city", "ward_id", "ward", "street", "display", "price_amount", "area_m2"}:
                            continue
                        remaining = original_filters[:index] + original_filters[index + 1:]
                        if not remaining:
                            continue
                        if retries >= 2:
                            break
                        retries += 1
                        candidate = await self.tools.search({**plan.arguments,
                            "filters": remaining})
                        if candidate.get("listingIds"):
                            search = candidate
                            ids = search["listingIds"]
                            relaxed = [field]
                            break
                if not ids:
                    alternative = await self._nearby_alternative(plan.arguments)
                    if alternative is not None:
                        alt_ids, alt_total, kind, wanted, place = alternative
                        alt_listings = [compact_search_listing(await self.tools.detail(value))
                                        for value in list(dict.fromkeys(alt_ids))[:limit]]
                        if alt_listings:
                            return AskResult(
                                answer=self._alternative_answer(kind, wanted, place, alt_total, alt_listings),
                                status="ANSWERED", citations=[], request_id=request_id,
                                listing_context=context_for([item["id"] for item in alt_listings]),
                                listing_ids=[item["id"] for item in alt_listings])
                if not ids:
                    return AskResult(answer="Mình chưa thấy tin đang hiển thị nào khớp đủ các điều kiện bạn vừa nêu. Bạn muốn nới tiêu chí nào (giá, khu vực hay tiện ích) để mình tìm tiếp?",
                                     status="NO_RESULTS", citations=[], request_id=request_id,
                                     listing_context=context_for([]))
                listings = [compact_search_listing(await self.tools.detail(listing_id)) for listing_id in list(dict.fromkeys(ids))[:limit]]
                requested_category = next((item.get("value") for item in plan.arguments.get("filters", [])
                                           if isinstance(item, dict) and item.get("field") == "category"
                                           and item.get("op") == "eq"), None)
                if requested_category:
                    listings = [item for item in listings if item.get("category") == requested_category]
                if not listings:
                    raise ListingToolError("Core search and detail disagree on listing category")
                if extras is not None and not selection:
                    extras["stretch"] = await self._stretch(plan.arguments, listings, requested_category)
                evidence: dict[str, Any] = {"total": search.get("total"), "readAt": search.get("readAt"),
                                             "filters": plan.arguments.get("filters"),
                                             "keyword": plan.arguments.get("keyword"),
                                             "relaxedFilters": relaxed, "listings": listings,
                                             "requestedCount": requested_count(question), "resultLimit": limit}
            elif plan.name == "get_listing_detail":
                listing = await self.tools.detail(plan.arguments["listingId"])
                evidence = {"readAt": "now", "listings": [listing]}
            else:
                ids = plan.arguments["listingIds"]
                if not 2 <= len(ids) <= max_results(self.settings):
                    return AskResult(answer=f'Mỗi lượt mình có thể so sánh tối đa {max_results(self.settings)} tin. Bạn chọn một nhóm nhỏ hơn nhé.',
                                     status='ANSWERED', citations=[], request_id=request_id, listing_context=context_for(scope))
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
        evidence["recentUserRequests"] = [item["content"][:1200] for item in history[-8:]
                                         if item.get("role") == "user"]
        evidence["scopeOnly"] = is_known_selection
        result_context = context_for(references)
        count_notice = self._count_notice(evidence) if plan.name == 'search_listings' else ''

        if plan.name == "search_listings" and (self._is_browse_query(question) or
                requested_count(question) is not None and not VietnamesePattern(
                    r'so sánh|phân tích|đánh giá|nên chọn|bao nhiêu|cho .*biết|chi tiết|tính thế nào').search(question)):
            return AskResult(answer=self._browse_answer(question, evidence), status="ANSWERED",
                             citations=[], request_id=request_id, listing_context=context_for(ids),
                             listing_ids=ids)

        if self._is_room_comparison(question, evidence["listings"]):
            return AskResult(answer=(count_notice + '\n' if count_notice else '') + self._room_comparison_answer(evidence["listings"], references),
                             status="ANSWERED", citations=[], request_id=request_id,
                             listing_context=result_context, listing_ids=ids)

        # A recommendation mentioning parking must not be reduced to a parking-only template.
        parking_answer = None if RECOMMENDATION.search(question) else self._parking_answer(question, evidence["listings"], references)
        if parking_answer is not None:
            if count_notice:
                parking_answer = count_notice + '\n' + parking_answer
            if evidence.get("relaxedFilters"):
                parking_answer = ("Chưa có tin khớp toàn bộ điều kiện. Những tin dưới đây chỉ là gợi ý sau khi bỏ tiêu chí "
                                  + relaxed_labels(evidence["relaxedFilters"]) + ".\n" + parking_answer)
            return AskResult(answer=parking_answer, status="ANSWERED", citations=[], request_id=request_id,
                             listing_context=result_context, listing_ids=ids)

        try:
            generated = await self.generative_client.generate_listing_answer(question=question, evidence=evidence)
            # Defense in depth: never expose a generated reference outside fresh public tool evidence.
            if any(value not in ids for value in generated.listing_ids):
                raise GenerationUnavailableError("Unknown listing reference.", retryable=False)
            raw = customer_listing_text(generated.answer)
            selected_ids = list(dict.fromkeys(generated.listing_ids))
            if plan.name == 'search_listings' and requested_count(question) is not None:
                selected_ids = ids  # Explicit page size applies to cards too, not just model prose references.
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

        if count_notice:
            raw = count_notice + '\n' + raw
        if evidence.get("relaxedFilters"):
            raw = ("Chưa có tin khớp toàn bộ điều kiện. Mình đã nới tiêu chí "
                   + relaxed_labels(evidence["relaxedFilters"]) + " để gợi ý các tin liên quan:\n" + raw)
        return AskResult(answer=raw.strip(),
                         status="ANSWERED", citations=[], request_id=request_id,
                         listing_context=result_context, listing_ids=selected_ids)

    _CATEGORY_NAMES = {"ROOM": "phòng trọ", "HOUSE": "nhà nguyên căn", "APARTMENT": "căn hộ"}

    async def _stretch(self, arguments: dict[str, Any], listings: list[dict[str, Any]],
                       category: str | None) -> dict[str, Any] | None:
        """One room just above budget (at most +500k) that is clearly larger than the in-budget rooms."""
        filters = [item for item in arguments.get("filters") or [] if isinstance(item, dict)]
        budget = stretch.budget_of(filters)
        if budget is None:
            return None
        try:
            found = await self.tools.search({**arguments, "filters": stretch.stretch_filters(filters, budget),
                                             "sort": "area_desc", "limit": 5})
            candidates = [compact_search_listing(await self.tools.detail(value))
                          for value in list(dict.fromkeys(found.get("listingIds") or []))[:5]]
        except (ListingToolError, HTTPException, ValueError, TypeError, KeyError) as error:
            logger.warning("listing_stretch_failed", error=type(error).__name__)
            return None  # Optional extra: never fail the main answer because of it.
        if category:
            candidates = [item for item in candidates if item.get("category") == category]
        pick = stretch.pick_standout(candidates, listings, budget)
        return {"listing": pick, "budget": budget, "inBudget": listings}

    @staticmethod
    def _with_stretch(result: AskResult | None, found: dict[str, Any] | None) -> AskResult | None:
        if result is None or not found or result.status != "ANSWERED" or not result.listing_ids:
            return result
        item = found["listing"]
        if item is None:
            result.answer = (result.answer.rstrip() + "\n\n" + stretch.no_standout_note(found["budget"]))
            return result
        if item["id"] in result.listing_ids:
            return result
        shown = list(result.listing_ids)
        result.answer = (result.answer.rstrip() + "\n\n"
                         + stretch.stretch_note(item, found["inBudget"], found["budget"], len(shown) + 1))
        result.listing_ids = shown + [item["id"]]
        context_ids = list((result.listing_context or {}).get("listingIds") or shown)
        result.listing_context = context_for(context_ids + [item["id"]])
        return result

    async def _nearby_alternative(self, arguments: dict[str, Any]):
        """Never dead-end: keep the user's location/budget and relax only category, then street.

        Returns (ids, total, kind, requested_category, place) or None. Results are clearly
        labelled as alternatives and are never presented as exact matches.
        """
        filters = [item for item in arguments.get("filters") or [] if isinstance(item, dict)]
        category = next((item.get("value") for item in filters
                         if item.get("field") == "category" and item.get("op") == "eq"), None)
        street = next((item.get("value") for item in filters if item.get("field") in {"street", "display"}), None)
        ward = next((item.get("value") for item in filters if item.get("field") == "ward"), None)
        place = street or ward or "khu vực bạn chọn"
        has_location = any(item.get("field") in {"street", "display", "ward", "ward_id"} for item in filters)
        if not has_location:
            return None
        if category:
            rest = [item for item in filters if item.get("field") != "category"]
            found = await self.tools.search({**arguments, "filters": rest})
            if found.get("listingIds"):
                return found["listingIds"], found.get("total"), "category", category, place
        if street:
            rest = [item for item in filters if item.get("field") not in {"street", "display"}]
            if any(item.get("field") in {"ward", "ward_id"} for item in rest):
                found = await self.tools.search({**arguments, "filters": rest})
                if found.get("listingIds"):
                    return found["listingIds"], found.get("total"), "street", category, ward or place
        if any(item.get("field") == "price_amount" for item in filters):
            # Keep location and type; show the cheapest real options so the customer can decide on budget.
            rest = [item for item in filters if item.get("field") != "price_amount"]
            found = await self.tools.search({**arguments, "filters": rest, "sort": "price_asc"})
            if found.get("listingIds"):
                return found["listingIds"], found.get("total"), "budget", category, place
        return None

    @classmethod
    def _alternative_answer(cls, kind: str, wanted: str | None, place: str, total: Any,
                            listings: list[dict[str, Any]]) -> str:
        names = {cls._CATEGORY_NAMES.get(item.get("category"), "tin đăng") for item in listings}
        actual = " / ".join(sorted(names))
        wanted_name = cls._CATEGORY_NAMES.get(wanted, "tin đăng")
        count = len(listings)
        if kind == "category":
            intro = (f"Hiện chưa có {wanted_name} nào đang hiển thị tại {place}. "
                     f"Tuy nhiên cùng khu vực này có {total or count} {actual} bạn có thể tham khảo:")
        elif kind == "budget":
            intro = (f"Hiện chưa có {wanted_name} nào trong mức giá bạn đặt tại {place}. "
                     f"Mình xếp {count} tin giá thấp nhất cùng khu vực để bạn cân nhắc:")
        else:
            intro = (f"Hiện chưa có {wanted_name} nào đúng tại {place}. "
                     f"Mình gợi ý {count} {wanted_name} ở khu vực lân cận trong cùng phường:")
        return (f"{intro}\nBạn xem {count} lựa chọn bên dưới nhé; đây là gợi ý thay thế, chưa khớp đủ yêu cầu ban đầu. "
                "Bạn muốn mình lọc tiếp theo ngân sách hay tiện ích nào, hoặc giữ đúng loại hình ban đầu và mở rộng khu vực?")

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
        return bool(VietnamesePattern(r"\s*(?:tìm|kiếm|cho (?:tôi|mình) xem)\b").match(question)) and not bool(
            VietnamesePattern(r"so sánh|bao nhiêu|cho (?:tôi|mình) biết|chi tiết|đặt cọc|tiền cọc|\bphí\b|"
                              r"nội thất|tiện ích|điều kiện thuê|lịch xem|tính thế nào").search(question))

    @staticmethod
    def _is_room_comparison(question: str, listings: list[dict[str, Any]]) -> bool:
        # A general follow-up comparison has a predictable compact answer. Questions about a
        # particular field still go to the grounded generator so every listing field remains askable.
        return (2 <= len(listings) <= ABSOLUTE_MAX_RESULTS
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
    def _count_notice(evidence: dict[str, Any]) -> str:
        requested = evidence.get('requestedCount')
        if not requested:
            return ''
        shown = len(evidence['listings'])
        limit = evidence.get('resultLimit', shown)
        if requested > limit:
            return f'Bạn yêu cầu {requested} tin; mỗi lượt hiện tối đa {limit} tin, nên mình hiển thị {shown} tin trước.'
        total = evidence.get('total', shown)
        if total < requested:
            return f'Bạn yêu cầu {requested} tin nhưng hiện chỉ có {total} tin khớp các tiêu chí.'
        return ''

    @staticmethod
    def _browse_answer(question: str, evidence: dict[str, Any]) -> str:
        listings = evidence["listings"]
        first = listings[0]
        category_name = {"ROOM": "phòng trọ", "HOUSE": "nhà nguyên căn", "APARTMENT": "căn hộ"}.get(
            first.get("category"), "tin đăng")
        wards = {item.get("address", {}).get("ward") for item in listings}
        ward = next(iter(wards)) if len(wards) == 1 else None
        total = evidence.get("total") or len(listings)
        if evidence.get("relaxedFilters"):
            relaxed = relaxed_labels(evidence["relaxedFilters"])
            intro = f"Mình chưa thấy tin khớp đủ mọi tiêu chí. Sau khi nới điều kiện {relaxed}, có lựa chọn gần với yêu cầu của bạn:"
        else:
            keyword = evidence.get("keyword")
            where = f" có nhắc đến “{keyword}”" if keyword else (f" tại {ward}" if ward else "")
            intro = f"Mình tìm thấy {total} {category_name} đang hiển thị{where}:"
        lines = [intro]
        shown = listings
        notice = ListingAsk._count_notice(evidence)
        if notice:
            lines.append(notice)
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
