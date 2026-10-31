"""Select RAG or realtime listing tools and ground listing answers in Core API data.

Renter criteria persist in a versioned state (see renter_search_state). Every hard criterion is
compiled by Core before LIMIT; an empty result is reported and asked about, never relaxed.
"""

import asyncio
import json
import re
import time
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from fastapi import HTTPException, Request
import structlog

from homespace_ai.application.listing_catalog import CatalogError, SearchCatalog
from homespace_ai.application.listing_context import current_scope, RECOMMENDATION, scoped_selection
from homespace_ai.application.listing_presentation import customer_listing_text, without_managed_choice
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.clients.generative import BaseGenerativeClient, GenerationUnavailableError
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner, ToolPlan
from homespace_ai.core.config import Settings
from homespace_ai.core.business_properties import value_advice_timeout_seconds
from homespace_ai.tools.listings import ListingSearchRejected, ListingToolError, ListingTools
from homespace_ai.application.vietnamese_text import VietnamesePattern, fold_text
from homespace_ai.application.listing_location import LocationClarification, catalog as admin_catalog, resolve_location_filters
from homespace_ai.application.listing_evidence import compact_search_listing
from homespace_ai.application.listing_limits import max_results, requested_count, search_limit
from homespace_ai.application.listing_table import (SHOW_REQUEST, TABLE_REQUEST, listing_table, requested_sections,
                                                    section_labels, strip_images, strip_tables)
from homespace_ai.application.nearby_listing_advice import NEARBY_INTENT, answer_nearby
from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.renter_criteria import normalize, boundary_policy
from homespace_ai.application.renter_dates import request_today
from homespace_ai.application import listing_value_advice as value_advice
from homespace_ai.application import renter_guidance as guidance
from homespace_ai.application.listing_recommendation_explanation import explain as explain_main_choice
from homespace_ai.application.amenity_intent import ambiguous_amenities


LISTING_SIGNAL = VietnamesePattern(
    r"\b(tìm|kiếm|còn|so sánh|phòng|căn hộ|chung cư|studio|nhà|trọ|chỗ ở|nơi ở|chỗ thuê|thuê|tin đăng|bài đăng|"
    r"gửi xe|nội thất|tiện ích|ngân sách|triệu|tr/tháng|giá rẻ|rẻ nhất|ở ghép|ở một mình|thang máy|ban công|"
    r"xem thêm|tin khác)\b")
SEARCH_INTENT = VietnamesePattern(
    r"\b(tìm|kiếm|còn phòng|phòng nào|căn nào|nhà nào|so sánh|cần thuê|muốn thuê|cần tìm|muốn tìm|"
    r"chỗ ở|nơi ở|chỗ thuê|cần chỗ|kiếm chỗ|xem thêm|tin khác)\b")
MORE_PAGE = VietnamesePattern(r"xem thêm|tin khác|trang (?:sau|tiếp)|tiếp theo|thêm (?:tin|phòng|căn|nhà)\b")
SUPPORT_INTENT = VietnamesePattern(r"(homespace là|cách đăng tin|hướng dẫn đăng tin|cách đặt lịch|cách thanh toán|điều khoản sử dụng|chính sách homespace|hợp đồng điện tử)")
MAP_EVIDENCE_REQUEST = VietnamesePattern(
    r"quét.{0,40}(?:xung quanh|tiện ích)|bán kính|khoảng cách (?:đến|tới)|"
    r"tiện ích (?:xung quanh|lân cận)|"
    r"gần (?:nhất|hơn).{0,40}(?:trường|đại học|siêu thị|bệnh viện|công viên|chợ|gym)|"
    r"(?:phòng|căn|nhà|tin) nào.{0,60}gần.{0,40}(?:trường|đại học|siêu thị|bệnh viện|công viên|chợ|gym)"
    r".{0,40}(?:hơn|nhất)\b")
CONFIRM = VietnamesePattern(r"^\s*(?:ok|oke|ừ|ừm|uh|đồng ý|được|được rồi|bỏ đi|bỏ|có|vâng|dạ|chấp nhận|đúng rồi)\b")
OP_TEXT = {"eq": "", "ne": "khác", "gt": "trên", "gte": "từ", "lt": "dưới", "lte": "tối đa", "contains": "có chứa",
           "in": "thuộc", "approx": "khoảng"}
ADVICE_REQUEST = VietnamesePattern(
    r"tư vấn|gợi ý|nên (?:chọn|thuê|lấy)|phù hợp nhất|tốt nhất|phân tích|đánh giá|nhận xét|"
    r"bao nhiêu|tính (?:thế nào|giúp|tổng)|tổng chi phí|cho (?:tôi|mình|em) biết|giải thích|tại sao|vì sao|"
    r"so với|khác (?:gì|nhau)|ưu (?:điểm|nhược)")
TABLE_HINT = ("Bấm tên hoặc ảnh để mở tin ở tab mới. Muốn so sánh thêm tiện ích, nội thất, chi phí, điều kiện thuê, "
              "nội quy hay lịch xem, bạn cứ nói, mình thêm vào bảng.")
logger = structlog.get_logger(__name__)


def vnd(value: Any) -> str:
    try:
        return f"{int(Decimal(str(value))):,}".replace(",", ".") + " đ"
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def describe(constraint: dict[str, Any], catalog: SearchCatalog | None) -> str:
    field, op, value = constraint["field"], constraint["op"], constraint["value"]
    label = catalog.label(field) if catalog else field
    if isinstance(value, bool) or str(value).lower() in {"true", "false"}:
        yes = (value is True or str(value).lower() == "true") == (op == "eq")
        text = f"{label}: {'có' if yes else 'không'}"
    else:
        if isinstance(value, list):
            shown = ", ".join(catalog.value_label(field, v) if catalog else str(v) for v in value)
        elif catalog and catalog.is_money(field):
            shown = vnd(value)
        else:
            shown = catalog.value_label(field, value) if catalog else str(value)
        text = f"{label} {OP_TEXT.get(op, op)} {shown}".replace("  ", " ")
    return text + (" (ưu tiên, không bắt buộc)" if constraint.get("strength") == "preference" else "")


def criteria_lines(state: dict[str, Any] | None, catalog: SearchCatalog | None) -> list[str]:
    if not state:
        return []
    lines = []
    location = state.get("location") or {}
    if location.get("ward") or location.get("city"):
        lines.append("Khu vực: " + ", ".join(v for v in (location.get("ward"), location.get("city")) if v))
    if state.get("target"):
        lines.append(f"Trong {state['target']['radiusMeters']} m (đường chim bay) quanh {state['target']['name']}")
    for item in state.get("nearby") or []:
        name = next((k.get("label") for k in (catalog.kinds if catalog else []) if k["code"] == item.get("kind")), None)
        lines.append(f"Có {name or item.get('kind') or item.get('group')} trong {item['radiusMeters']} m")
    lines.extend(describe(c, catalog) for c in state["constraints"])
    for index, group in enumerate(state.get("alternatives") or [], 1):
        lines.append(f"Lựa chọn {index}: " + "; ".join(describe(c, catalog) for c in group))
    labels = {"occupants": "người ở", "motorbikes": "xe máy", "cars": "ô tô", "electricityKwh": "kWh điện/tháng", "waterM3": "m³ nước/tháng"}
    context = {k: v for k, v in (state.get("context") or {}).items() if k in labels and v}
    if context:
        lines.append("Thông tin của bạn: " + ", ".join(f"{v} {labels[k]}" for k, v in context.items()))
    return lines


def pick_candidate(question: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A short reply chooses one of the candidates Core returned; never a new guess."""
    if not candidates:
        return None
    text = fold_text(question)
    ordinal = re.search(r"\b(?:co so|so|thu|lua chon|cai|dia diem)?\s*(\d)\b", text)
    words = {"thu nhat": 1, "dau tien": 1, "thu hai": 2, "thu ba": 3, "thu tu": 4}
    index = next((n for w, n in words.items() if w in text), None) or (int(ordinal[1]) if ordinal and len(text) <= 40 else None)
    if index is not None and 1 <= index <= len(candidates):
        return candidates[index - 1]
    matches = [c for c in candidates if any(len(token) >= 4 and token in text for token in
               re.findall(r"[a-z0-9]+(?: [a-z0-9]+){1,2}", fold_text(c.get("address") or "")))]
    return matches[0] if len(matches) == 1 else None


class ListingAsk:
    def __init__(self, settings: Settings, generative_client: BaseGenerativeClient,
                 gateway: GatewayApiClient, request: Request, clock: Callable[[], datetime] | None = None) -> None:
        self.settings = settings
        self.generative_client = generative_client
        self.tools = ListingTools(gateway, request, max_results(settings))
        self.planner = ListingToolPlanner(settings)
        self.clock = clock
        self.catalog: SearchCatalog | None = None
        self._base: dict[str, Any] | None = None
        self._require_search_basics = True

    def today(self):
        return request_today(self.clock, getattr(self.settings, "listing_search_utc_offset", None))

    def context(self, ids: list[str], state: dict[str, Any] | None) -> dict[str, Any]:
        """listingContext v2 with the criteria state; the revision moves only when the state changed."""
        if state is None:
            return rs.listing_context(ids, None)
        state = deepcopy(state)
        base = self._base or rs.empty_state()
        comparable = {k: v for k, v in state.items() if k != "revision"}
        if comparable != {k: v for k, v in base.items() if k != "revision"} or self._base is None:
            state["revision"] = base["revision"] + 1
        else:
            state["revision"] = base["revision"]
        return rs.listing_context(ids, state)

    async def try_answer(self, question: str, history: list[dict[str, Any]], request_id: str,
                         state: dict[str, Any] | None = None) -> AskResult | None:
        state = rs.sanitize(state) if state is not None else rs.latest_state(history)
        self._base = deepcopy(state)
        profile = guidance.profile_update(question)
        if profile:
            state = rs.merge(state, {"renterProfile": profile}, question)
        help_answer = guidance.help_answer(question)
        if help_answer:
            kept = deepcopy(state) if state else rs.empty_state()
            field, text = help_answer
            kept["pending"] = {**self._pending("INTAKE", text), "fields": [field]}
            result = self._ask(request_id, current_scope(history), kept, text)
            result.suggested_questions = guidance.suggested_questions(kept, has_results=bool(current_scope(history)))
            return result
        policy = boundary_policy(question)
        if policy is not None and state is not None:
            state = deepcopy(state)
            state["boundaryAdvice"] = policy  # Fact/detail shortcuts must also preserve an explicit opt-out.
        if SUPPORT_INTENT.search(question) and not SEARCH_INTENT.search(question):
            return None
        if MAP_EVIDENCE_REQUEST.search(question) and not getattr(self.settings, "nearby_query_enabled", False):
            return AskResult(
                answer="Hiện mình chưa có dữ liệu quét tiện ích xung quanh hoặc đo khoảng cách để xếp hạng phòng theo vị trí. "
                       "Mình vẫn có thể giúp tìm và so sánh phòng theo khu vực, giá thuê, diện tích và tiện nghi có trong tin đăng.",
                status="ANSWERED", citations=[], request_id=request_id,
                listing_context=self.context(current_scope(history), state))
        result = await self._listing_flow(question, history, request_id, state)
        if result is not None and not result.suggested_questions:
            context = result.listing_context or {}
            result.suggested_questions = guidance.suggested_questions(context.get("searchState"), has_results=bool(result.listing_ids),
                has_boundary=bool(context.get("valueSuggestion")), has_main=bool(context.get("mainRecommendation")), no_results=result.status == "NO_RESULTS")
        return result

    async def _listing_flow(self, question: str, history: list[dict[str, Any]], request_id: str,
                            state: dict[str, Any] | None) -> AskResult | None:
        scope = current_scope(history)
        selection = scoped_selection(question, history)
        if scope and guidance.profile_only_reply(question):
            # This answers our own purpose question. Keep the shortlist and factual
            # filters; a persona does not imply children, bedrooms, amenities or keywords.
            return await self._continue_advice(question, history, request_id, scope, state)
        if scope and ambiguous_amenities(question):
            return AskResult(answer="Bạn muốn xem tiện ích trong tòa nhà (như WiFi, thang máy) hay địa điểm xung quanh (như siêu thị, trường học)?",
                status="NEEDS_CLARIFICATION", citations=[], request_id=request_id, listing_context=self.context(scope, state),
                suggested_questions=["Các phòng trên có những tiện ích nào xung quanh?", "Các phòng trên có những tiện ích tòa nhà nào?"])
        chosen = None
        if guidance.MAIN_REFERENCE.search(fold_text(question)) and not SEARCH_INTENT.search(question):
            latest = next((item.get("listingContext") for item in reversed(history)
                           if item.get("role") == "assistant" and item.get("listingContext")), {})
            reference = latest.get("mainRecommendation") or {}
            if reference.get("listingId") in scope:
                chosen = reference
                selection = scope  # Explain against the whole shortlist, not one isolated record.
        if value_advice.VALUE_REFERENCE.search(question) and not SEARCH_INTENT.search(question):
            latest = next((item.get("listingContext") for item in reversed(history)
                           if item.get("role") == "assistant" and item.get("listingContext")), {})
            identifier = (latest.get("valueSuggestion") or {}).get("listingId")
            if not identifier and state:
                previous_suggestions = rs.boundary_exclusions(state)
                identifier = previous_suggestions[-1] if previous_suggestions else None
            if identifier:
                selection = [identifier]  # Server-stored reference; never a fifth ordinal in the original table.
            else:
                return AskResult(answer="Bạn muốn xem tin gợi ý nào? Bạn gửi lại đường dẫn hoặc nhắc đặc điểm của tin đó nhé.",
                                 status="ANSWERED", citations=[], request_id=request_id,
                                 listing_context=self.context(scope, state))
        pending = (state or {}).get("pending")
        if selection == []:
            return self._clarify(request_id, scope, state)
        if selection is None and not guidance.profile_update(question) and requested_count(question) is None and not LISTING_SIGNAL.search(question) and not pending and not any(
                LISTING_SIGNAL.search(item.get("content", "")) for item in history[-2:]):
            return None
        if pending and pending["type"] == "TARGET" and pending["candidates"]:
            choice = pick_candidate(question, pending["candidates"])
            if choice is not None:
                chosen = deepcopy(state)
                chosen["target"] = {**choice, "radiusMeters": pending.get("radiusMeters") or self.settings.nearby_default_radius_m}
                chosen["pending"] = None
                return await self._search(question, history, request_id, chosen, chosen, scope, {})
        if pending and pending["type"] == "CATEGORY_CHANGE" and pending.get("proposed") and CONFIRM.search(question):
            proposed = deepcopy(pending["proposed"])
            proposed["pending"] = None
            return await self._search(question, history, request_id, proposed, proposed, scope, {})
        if chosen:
            return await self._explain_chosen(question, request_id, chosen, scope, state, history)
        stage = "field_catalog"
        try:
            nearby_memory = next((item.get("listingContext", {}).get("nearby") for item in reversed(history)
                                  if item.get("role") == "assistant" and isinstance(item.get("listingContext"), dict)), None)
            chosen_nearby = chosen and any(f.startswith("nearby.") for f in chosen.get("fields", []))
            if chosen_nearby and nearby_memory:
                chosen = None  # Provider facts must be re-read by the nearby tool, not a detail-only explanation.
                plan = ToolPlan("get_listing_nearby_places", {**nearby_memory, "listingIds": selection})
            elif selection and not chosen and nearby_memory and getattr(self.settings, "nearby_query_enabled", False) and RECOMMENDATION.search(question) and not NEARBY_INTENT.search(question) and not VietnamesePattern(r"giá|chi phí|rẻ|nội thất|diện tích").search(question):
                plan = ToolPlan("get_listing_nearby_places", {**nearby_memory, "listingIds": selection})
            elif selection and not (getattr(self.settings, "nearby_query_enabled", False) and NEARBY_INTENT.search(question)) and not rs.RESET.search(question):
                # Reading known references needs neither the catalog nor a paid planner call, and never edits criteria.
                plan = ToolPlan("get_listing_detail", {"listingId": selection[0]}) if len(selection) == 1 else (
                    ToolPlan("get_listing_details", {"listingIds": selection}))
            else:
                self.catalog = SearchCatalog(await self.tools.catalog())
                stage = "tool_planning"
                plan = await self.planner.plan(question, history, self.catalog, state, self.today())
        except CatalogError as error:
            self._log_failure(request_id, stage, error, reason=error.reason)
            return self._unavailable(request_id, scope, state)
        except (ListingToolError, GenerationUnavailableError, HTTPException) as error:
            self._log_failure(request_id, stage, error)
            if LISTING_SIGNAL.search(question) or pending or any(LISTING_SIGNAL.search(item.get("content", "")) for item in history[-2:]):
                return self._unavailable(request_id, scope, state)
            return None
        if plan is None:
            logger.info("listing_plan_outcome", request_id=request_id, outcome="NO_TOOL", scope_count=len(scope))
            if SEARCH_INTENT.search(question) or pending or (scope and LISTING_SIGNAL.search(question)):
                return self._clarify(request_id, scope, state)
            return None  # Knowledge question: preserve the existing RAG behavior.

        logger.info("listing_tool_selected", request_id=request_id, tool=plan.name,
                    scope_count=len(scope), scoped_followup=bool(selection))
        if (selection and NEARBY_INTENT.search(question) and getattr(self.settings, "nearby_query_enabled", False)
                and plan.name in {"get_listing_detail", "get_listing_details"}):
            # Explicit outside-place questions cannot be answered with inside-building amenities.
            plan = ToolPlan("get_listing_nearby_places", {"listingIds": selection,
                "radiusMeters": self.settings.nearby_default_radius_m, "groups": []})
        if plan.name == "ask_clarification":
            return await self._model_clarification(question, history, request_id, scope, state, plan.arguments)
        if plan.name == "get_listing_nearby_places":
            return await answer_nearby(self, question, history, request_id, plan.arguments, state=state)
        if plan.name == "search_listings":
            try:
                update = normalize(question, plan.arguments, self.catalog, state, self.today())
            except CatalogError as error:
                self._log_failure(request_id, "plan_validation", error, plan.name, reason=error.reason)
                return self._plan_rejected(request_id, scope, state, error.reason, error.field, error.allowed)
            return await self._apply(question, history, request_id, state, update, scope)
        stage = "tool_execution"
        try:
            if plan.name == "get_listing_detail":
                evidence = {"readAt": "now", "listings": [await self.tools.detail(plan.arguments["listingId"])]}
            else:
                ids = plan.arguments["listingIds"]
                if not 2 <= len(ids) <= max_results(self.settings):
                    return AskResult(answer=f'Mỗi lượt mình có thể so sánh tối đa {max_results(self.settings)} tin. Bạn chọn một nhóm nhỏ hơn nhé.',
                                     status='ANSWERED', citations=[], request_id=request_id, listing_context=self.context(scope, state))
                listings, gone = await self._details(ids, request_id)
                if not listings:
                    return AskResult(answer="Các tin bạn chọn hiện không còn hiển thị công khai, nên mình không dùng dữ liệu cũ để trả lời. Bạn muốn tìm lại theo tiêu chí đã lưu không?",
                                     status="NO_RESULTS", citations=[], request_id=request_id, listing_context=self.context([], state))
                evidence = {"readAt": "now", "listings": listings, "unavailableListings": len(gone)}
        except (ListingToolError, HTTPException, ValueError, TypeError, KeyError) as error:
            self._log_failure(request_id, stage, error, plan.name)
            if isinstance(error, ListingToolError) and error.status_code == 404 or str(error) == "Listing is no longer public":
                return AskResult(answer="Tin này hiện không còn hiển thị công khai, nên mình không dùng thông tin cũ để trả lời.",
                                 status="NO_RESULTS", citations=[], request_id=request_id, listing_context=self.context(scope, state))
            return self._unavailable(request_id, scope, state)
        result = await self._present(question, history, request_id, plan.name, evidence, scope, state, ids=None)
        if len(evidence["listings"]) > 1:
            displayed = [item for item in evidence["listings"] if item["id"] in result.listing_ids]
            result = await self._advise_main(question, request_id, result, displayed, state)
        return result

    async def _explain_chosen(self, question, request_id, chosen, scope, state, history):
        nearby=next((m.get("listingContext",{}).get("nearby") for m in reversed(history)
                     if m.get("role")=="assistant" and isinstance(m.get("listingContext"),dict)),None)
        try:
            async with asyncio.timeout(value_advice_timeout_seconds()):
                listings, gone = await self._details(scope, request_id)
                if not any(item["id"] == chosen["listingId"] for item in listings):
                    return AskResult(answer="Phòng được gợi ý trước đó hiện không còn hiển thị công khai. Bạn muốn mình đối chiếu lại các phòng còn lại không?",
                        status="NO_RESULTS", citations=[], request_id=request_id, listing_context=self.context(scope,state))
                if self.catalog is None:
                    self.catalog = SearchCatalog(await self.tools.catalog())
                location_facts=None
                if nearby and any(field.startswith("nearby.") for field in chosen.get("fields",[])):
                    from homespace_ai.application.nearby_table import facts as nearby_facts
                    evidence=await self.tools.nearby([item["id"] for item in listings],nearby["radiusMeters"],nearby["groups"],nearby.get("targetPlaceId"))
                    location_facts=nearby_facts(evidence["summaries"],radius=nearby["radiusMeters"],profile=(state or {}).get("renterProfile"))
                text = await explain_main_choice(question, chosen, listings,
                    (state or rs.empty_state()) | {"requestToday": self.today().isoformat()}, scope, self.catalog, self.generative_client,location_facts)
            context=self.context(scope,state) | {"mainRecommendation":chosen}
            if nearby:
                context["nearby"]=nearby
            return AskResult(answer=text or "Bạn muốn mình giải thích rõ hơn về giá, không gian hay tiện nghi của phòng được chọn?",
                status="ANSWERED",citations=[],request_id=request_id,listing_context=context,listing_ids=[item["id"] for item in listings])
        except (CatalogError,ListingToolError,GenerationUnavailableError,HTTPException,TimeoutError,ValueError,TypeError,KeyError) as error:
            self._log_failure(request_id,"explain_main",error)
            return self._unavailable(request_id,scope,state)

    async def _advise_main(self, question: str, request_id: str, answer: AskResult, listings: list[dict],
                           state: dict | None, *, nearby_facts: dict | None = None) -> AskResult:
        """One current-scope recommendation on follow-ups, with no boundary or provider calls."""
        if answer.status != "ANSWERED" or not listings or not hasattr(self.generative_client, "generate_value_advice"):
            return answer
        criteria = state or rs.empty_state()
        try:
            async with asyncio.timeout(value_advice_timeout_seconds()):
                if self.catalog is None:
                    self.catalog = SearchCatalog(await self.tools.catalog())
                advice = await value_advice.evaluate(question, criteria | {"requestToday": self.today().isoformat()}, listings,
                    [], {"candidateIds": []}, self.generative_client, self.catalog, recommend_main=True, nearby_facts=nearby_facts)
            main = (advice or {}).get("main")
            if main:
                answer.answer += "\n\n" + value_advice.render_main(main, listings, answer.listing_context["listingIds"])
                answer.listing_context["mainRecommendation"] = {"listingId": main["listingId"],
                    "fields": [reason["field"] for reason in main["reasons"]]}
        except (CatalogError, ListingToolError, GenerationUnavailableError, HTTPException, TimeoutError, ValueError, TypeError, KeyError) as error:
            self._log_failure(request_id, "main_recommendation", error)
        followup = guidance.followup_question(criteria)
        if followup:
            answer.answer += "\n\n" + followup
            criteria = deepcopy(criteria)
            criteria["guidanceAsked"] = list(dict.fromkeys((criteria.get("guidanceAsked") or []) + [followup]))[-6:]
            references = {k: answer.listing_context[k] for k in ("mainRecommendation", "nearby") if k in answer.listing_context}
            answer.listing_context = self.context(answer.listing_context["listingIds"], criteria) | references
        return answer

    async def _apply(self, question: str, history: list[dict[str, Any]], request_id: str, state: dict[str, Any] | None,
                     update: dict[str, Any], scope: list[str]) -> AskResult:
        """Merge the update, resolve location/target and gate the search; nothing is searched on a guess."""
        catalog = self.catalog
        radius_default = self.settings.nearby_default_radius_m
        merged = rs.merge(state, {
            "constraints": update["constraints"], "alternatives": update["alternatives"],
            "boundaryAdvice": update.get("boundaryAdvice"),
            "needs": update.get("needs") or [], "valuePriorities": update.get("valuePriorities") or [],
            "renterProfile": update.get("renterProfile") or {}, "intakeOpen": update.get("intakeOpen") or [],
            "removeFields": update["removeFields"], "nationwide": update["nationwide"],
            "nearby": [{**n, "radiusMeters": n["radiusMeters"] or radius_default} for n in update["nearby"]],
            "context": update["context"], "sort": update["sort"], "keyword": update["keyword"],
            "unsupported": update["unsupported"], "targetRadiusMeters": update["placeRadiusMeters"],
        }, question)
        if update.get("location"):
            try:
                merged["location"] = self._resolve_location(update["location"], question, history)
                merged["nationwide"] = False
            except LocationClarification as error:
                pending = {**merged, "pending": self._pending("LOCATION", str(error))}
                return self._ask(request_id, scope, pending, str(error))
        if update.get("placeQuery"):
            if not getattr(self.settings, "nearby_query_enabled", False):
                return self._ask(request_id, scope, merged, "Hiện mình chưa có dữ liệu vị trí để lọc theo khoảng cách tới địa điểm. Bạn cho mình phường/xã và tỉnh/thành muốn tìm nhé.")
            radius = update["placeRadiusMeters"] or (merged["target"] or {}).get("radiusMeters") or radius_default
            try:
                candidates = await self.tools.resolve_place(update["placeQuery"])
            except (ListingToolError, HTTPException) as error:
                self._log_failure(request_id, "place_resolution", error)
                return self._unavailable(request_id, scope, state)
            if len(candidates) != 1:
                merged["target"] = None
                question_text = (f"Mình thấy {len(candidates)} địa điểm khớp “{update['placeQuery']}”. Bạn muốn tìm quanh cơ sở nào?\n"
                                 + "\n".join(f"{i}. {c['name']}" + (f" — {c['address']}" if c.get("address") else "")
                                             for i, c in enumerate(candidates, 1))) if candidates else (
                    f"Mình chưa tìm thấy “{update['placeQuery']}” trong kho địa điểm hiện có, nên chưa lọc theo khoảng cách được. "
                    "Bạn cho mình tên đầy đủ hoặc cơ sở cụ thể, hoặc phường/xã muốn tìm nhé.")
                merged["pending"] = {**self._pending("TARGET", question_text), "candidates": candidates, "radiusMeters": radius}
                return self._ask(request_id, scope, merged, question_text, data_sources=["CORE_NEARBY"])
            merged["target"] = {**candidates[0], "radiusMeters": radius}
        found = rs.conflicts(merged)
        if found:
            item = found[0]
            label = catalog.label(item["field"]) if catalog else item["field"]
            text = (f"Mình thấy hai điều kiện về {label.lower()} mâu thuẫn: {describe(item['lower'], catalog)} và {describe(item['upper'], catalog)}. "
                    "Bạn muốn chốt mức nào? Các tiêu chí khác mình vẫn giữ nguyên.")
            merged["pending"] = {**self._pending("CONFLICT", text), "fields": [item["field"]]}
            return self._ask(request_id, scope, merged, text)
        stale = rs.incompatible(merged)
        if stale:
            names = "; ".join(describe(c, catalog) for c in stale)
            text = (f"Loại hình bạn vừa chọn không áp dụng được các điều kiện: {names}. "
                    "Bạn đồng ý bỏ các điều kiện này để tìm tiếp, hay muốn giữ loại hình cũ?")
            merged["pending"] = {**self._pending("CATEGORY_CHANGE", text), "fields": [c["field"] for c in stale],
                                 "proposed": rs.without(merged, stale)}
            return self._ask(request_id, scope, merged, text)
        if update.get("dateAmbiguous") and not any(c["field"] == "available_from" for c in merged["constraints"]):
            text = "Bạn dự định dọn vào khoảng ngày nào (ví dụ đầu tháng sau hay một ngày cụ thể)? Mình lọc theo ngày có thể nhận phòng."
            merged["pending"] = self._pending("DATE", text)
            return self._ask(request_id, scope, merged, text)
        within = bool(update.get("withinDisplayed") and scope)
        if not within and not rs.has_scope(merged) and not getattr(self, "_require_search_basics", True):
            text = ("Bạn muốn tìm ở khu vực nào? Cho mình phường/xã và tỉnh/thành phố "
                    "(ví dụ: Phường Hạnh Thông, Thành phố Hồ Chí Minh), hoặc tên trường/nơi làm việc cụ thể nhé.")
            merged["pending"] = self._pending("LOCATION", text)
            return self._ask(request_id, scope, merged, text)
        persist = deepcopy(state) if update.get("hypothetical") and state else merged
        if (scope and state and update.get("renterProfile")
                and rs.fingerprint(merged | {"sort": merged.get("sort") or "newest"}) == rs.fingerprint(state | {"sort": state.get("sort") or "newest"})
                and not update.get("moreResults") and not MORE_PAGE.search(question)
                and requested_count(question) is None and not rs.RESET.search(question)):
            # A profile answer may contain "muốn thuê". The unchanged query, not a search
            # verb alone, determines whether to keep advising on the displayed shortlist.
            return await self._continue_advice(question, history, request_id, scope, persist)
        return await self._search(question, history, request_id, merged, persist, scope, update)

    async def _continue_advice(self, question, history, request_id, scope, state):
        try:
            listings, _ = await self._details(scope, request_id)
            if not listings:
                return AskResult(answer="Các phòng vừa xem hiện không còn hiển thị công khai. Bạn muốn mình tìm lại theo các tiêu chí đã lưu không?",
                    status="NO_RESULTS", citations=[], request_id=request_id, listing_context=self.context([], state))
            evidence = {"readAt": "now", "listings": listings}
            result = await self._present(question, history, request_id, "get_listing_details", evidence, scope, state, ids=None)
            if len(listings) > 1:
                result = await self._advise_main(question, request_id, result, listings, state)
            return result
        except (ListingToolError, GenerationUnavailableError, HTTPException) as error:
            self._log_failure(request_id, "profile_followup", error)
            return self._unavailable(request_id, scope, state)

    def _resolve_location(self, location: dict[str, Any], question: str, history: list[dict[str, Any]]) -> dict[str, str]:
        filters = [{"field": "ward", "op": "eq", "value": location["ward"]}] if location.get("ward") else []
        if location.get("city"):
            filters.append({"field": "city", "op": "eq", "value": location["city"]})
        resolved = resolve_location_filters(filters, question, history, {"ward_id": "TEXT", "city_id": "TEXT"})
        cities, wards, _ = admin_catalog()
        result: dict[str, str] = {}
        for item in resolved:
            if item["field"] == "ward_id":
                result["wardId"], result["ward"] = item["value"], wards[item["value"]][1]
            elif item["field"] == "city_id":
                result["cityId"], result["city"] = item["value"], cities[item["value"]]
        if not result:
            raise LocationClarification("Mình chưa xác định được khu vực bạn nói. Bạn cho mình phường/xã và tỉnh/thành phố nhé.")
        return result

    async def _search(self, question: str, history: list[dict[str, Any]], request_id: str, criteria: dict[str, Any],
                      persist: dict[str, Any], scope: list[str], update: dict[str, Any]) -> AskResult:
        # The page size is the customer's stated count (or the default); a model limit may only shrink it.
        limit = search_limit(question, self.settings)
        if not requested_count(question) and update.get("limit"):
            limit = min(limit, update["limit"])
        within = bool(update.get("withinDisplayed") and scope)
        if not within and getattr(self, "_require_search_basics", True):
            missing = guidance.missing_basics(criteria)
            if missing:
                pending = deepcopy(criteria)
                pending["pending"] = {**self._pending("INTAKE", ""), "fields": missing[:2]}
                answer = self._ask(request_id, scope, pending, guidance.intake_question(missing))
                answer.status = "NEEDS_CLARIFICATION"
                answer.suggested_questions = guidance.suggested_questions(pending)
                return answer
        more = bool(update.get("moreResults") or MORE_PAGE.search(question))
        current = rs.fingerprint(criteria)
        exclude = criteria["shown"]["ids"] if more and criteria["shown"]["fingerprint"] == current else []
        payload = rs.core_request(criteria, limit=limit, scope_ids=scope if within else None, exclude_ids=exclude)
        started = time.perf_counter()
        try:
            result = await self.tools.search_v2(payload)
        except ListingSearchRejected as error:
            self._log_failure(request_id, "core_search", error, "search_listings", reason=error.reason)
            if error.reason == "TARGET_UNAVAILABLE":
                retry = deepcopy(persist)
                retry["target"] = None
                text = "Địa điểm đã chọn hiện không còn hợp lệ trong kho dữ liệu, nên mình chưa lọc theo khoảng cách được. Bạn chọn lại trường/cơ sở nhé."
                retry["pending"] = self._pending("TARGET", text)
                return self._ask(request_id, scope, retry, text, data_sources=["CORE_NEARBY"])
            return self._plan_rejected(request_id, scope, self._base, error.reason, error.field, error.allowed)
        except (ListingToolError, HTTPException, ValueError, TypeError, KeyError) as error:
            self._log_failure(request_id, "core_search", error, "search_listings")
            return self._unavailable(request_id, scope, self._base)
        logger.info("listing_search_result", request_id=request_id, outcome="RESULTS" if result["listingIds"] else "NO_RESULTS",
                    filters=len(payload["where"]["all"]), nearby=len(payload.get("nearbyAll") or []), total=result["total"],
                    unknown=result["unknownTotal"], returned=len(result["listingIds"]), within_scope=within, more=more,
                    payload_bytes=len(json.dumps(payload, ensure_ascii=False).encode()), latency_ms=round((time.perf_counter() - started) * 1000))
        ids = result["listingIds"]
        if not ids:
            return self._no_results(request_id, persist, criteria, result, more=bool(exclude))
        full, gone = await self._details(ids, request_id)
        allowed = rs.categories(criteria)
        if allowed:
            full = [item for item in full if item.get("category") in allowed]
        listings = [compact_search_listing(item) for item in full]
        if not listings:
            return self._no_results(request_id, persist, criteria, {**result, "total": 0}, changed=len(gone))
        shown_ids = [item["id"] for item in listings]
        persist = rs.record_shown(persist, shown_ids, more=more)
        checks = {item["listingId"]: item for item in result["evidence"]}
        for item in listings:
            proof = checks.get(item["id"])
            if proof:
                item["criteriaChecks"] = [{"criterion": self._check_label(c["fields"]), "status": c["status"], "actual": c["actual"]}
                                          for c in proof["checks"]]
                if proof.get("targetDistanceMeters") is not None:
                    item["targetDistanceMeters"] = proof["targetDistanceMeters"]
        evidence: dict[str, Any] = {
            "total": result["total"], "unknownTotal": result["unknownTotal"], "readAt": result.get("readAt"),
            "requestedCriteria": criteria_lines(criteria, self.catalog),
            "preferences": [describe(c, self.catalog) for c in criteria["constraints"] if c["strength"] == "preference"],
            "unsupportedNeeds": [next((u.get("reason") for u in (self.catalog.unsupported if self.catalog else []) if u["code"] == x["code"]), x["code"])
                                 for x in criteria.get("unsupported") or []],
            "contextMissing": result["contextMissing"], "listings": listings,
            "target": {"name": criteria["target"]["name"], "distanceType": "STRAIGHT_LINE"} if criteria.get("target") else None,
            "requestedCount": requested_count(question), "resultLimit": limit, "unavailableListings": len(gone),
            "withinDisplayed": within, "moreResults": more, "keyword": criteria.get("keyword"),
            "shownSoFar": len(persist["shown"]["ids"]) if more else len(listings),
        }
        distances = {item["id"]: item["targetDistanceMeters"] for item in listings if item.get("targetDistanceMeters") is not None}
        answer = await self._present(question, history, request_id, "search_listings", evidence, scope, persist, ids=shown_ids,
                                     table_source=full, distances=distances)
        seen_boundary = rs.boundary_exclusions(persist)
        displayed_ids = answer.listing_ids
        displayed = [item for identifier in displayed_ids for item in full if item["id"] == identifier]
        allow_boundary = rs.boundary_turn(self._base, criteria, more=more, within=within)
        if not within:
            persist["boundaryArea"] = rs.boundary_area(criteria)
            answer.listing_context = self.context(answer.listing_ids, persist)
        if answer.status == "ANSWERED" and displayed and hasattr(self.generative_client, "generate_value_advice"):
            try:
                async with asyncio.timeout(value_advice_timeout_seconds()):
                    boundary, candidates = {"candidateIds": []}, []
                    if allow_boundary and len(seen_boundary) < 40 and value_advice.eligible(question, criteria, payload, displayed, more=more, within=within):
                        try:
                            boundary = await self.tools.value_opportunity(payload, displayed_ids, seen_boundary)
                            semaphore = asyncio.Semaphore(4)
                            async def read_candidate(identifier):
                                async with semaphore:
                                    try:
                                        return await self.tools.detail(identifier)
                                    except ListingToolError as error:
                                        if error.status_code in {404, 410} or str(error) == "Listing is no longer public":
                                            return None
                                        raise
                            candidates = [item for item in await asyncio.gather(
                                *(read_candidate(identifier) for identifier in boundary["candidateIds"])) if item is not None]
                        except (ListingToolError, HTTPException) as error:
                            self._log_failure(request_id, "boundary_candidates", error, "find_value_opportunity")
                            boundary, candidates = {"candidateIds": []}, []
                    advice = await value_advice.evaluate(question, criteria | {"requestToday": self.today().isoformat()}, displayed,
                        candidates, boundary, self.generative_client, self.catalog, recommend_main=True)
                main, suggestion = (advice or {}).get("main"), (advice or {}).get("boundary")
                if main:
                    answer.answer += "\n\n" + value_advice.render_main(main, displayed)
                    answer.listing_context["mainRecommendation"] = {"listingId": main["listingId"],
                        "fields": [reason["field"] for reason in main["reasons"]]}
                if suggestion:
                    answer.answer += "\n\n" + value_advice.render(suggestion, displayed, boundary)
                    persist = rs.record_boundary(persist, suggestion["listingId"])
                    answer.listing_context["valueSuggestion"] = {"listingId": suggestion["listingId"]}
                logger.info("listing_value_comparison", request_id=request_id, suggested=bool(suggestion), main_selected=bool(main), displayed=len(displayed))
            except (ListingToolError, GenerationUnavailableError, HTTPException, TimeoutError, ValueError, TypeError, KeyError) as error:
                # Optional advice failing must never discard valid main results or widen criteria.
                self._log_failure(request_id, "value_comparison", error, "find_value_opportunity")
            followup = guidance.followup_question(persist)
            if followup:
                answer.answer += "\n\n" + followup
                persist["guidanceAsked"] = list(dict.fromkeys((persist.get("guidanceAsked") or []) + [followup]))[-6:]
            references = {key: answer.listing_context[key] for key in ("mainRecommendation", "valueSuggestion") if key in answer.listing_context}
            answer.listing_context = self.context(answer.listing_ids, persist) | references
        return answer

    def _check_label(self, fields: list[str]) -> str:
        if not fields:
            return "điều kiện"
        first = fields[0]
        if first.startswith("nearby.target:"):
            return "khoảng cách tới địa điểm đã chọn"
        if first.startswith("nearby."):
            code = first.partition(".")[2]
            return next((k.get("label") for k in (self.catalog.kinds if self.catalog else []) if k["code"] == code), "tiện ích xung quanh")
        return "; ".join(self.catalog.label(f) if self.catalog else f for f in fields)

    async def _details(self, ids: list[str], request_id: str) -> tuple[list[dict[str, Any]], list[str]]:
        """Fresh public facts. A listing hidden/expired since the search is skipped, not fatal."""
        listings, gone = [], []
        for value in list(dict.fromkeys(ids)):
            try:
                listings.append(await self.tools.detail(value))
            except ListingToolError as error:
                if error.status_code in {404, 410} or str(error) == "Listing is no longer public":
                    gone.append(value)
                    continue
                raise
        if gone:
            logger.info("listing_detail_skipped", request_id=request_id, skipped=len(gone))
        return listings, gone

    async def _present(self, question: str, history: list[dict[str, Any]], request_id: str, tool: str,
                       evidence: dict[str, Any], scope: list[str], state: dict[str, Any] | None,
                       ids: list[str] | None, table_source: list[dict[str, Any]] | None = None,
                       distances: dict[str, int] | None = None) -> AskResult:
        ids = ids or [item["id"] for item in evidence["listings"]]
        is_known_selection = bool(scope) and tool != "search_listings" and all(value in scope for value in ids)
        references = scope if is_known_selection else ids
        evidence["selectionScope"] = [{"listingId": value, "position": index} for index, value in enumerate(references, 1)]
        evidence["recentUserRequests"] = [item["content"][:1200] for item in history[-8:] if item.get("role") == "user"]
        evidence["scopeOnly"] = is_known_selection
        evidence["mainRecommendationManaged"] = len(evidence["listings"]) > 1
        if state:
            evidence["confirmedCriteria"] = {key: state.get(key) for key in
                ("constraints", "context", "needs", "valuePriorities", "renterProfile")}
        result_context = self.context(references, state)
        count_notice = self._count_notice(evidence) if tool == 'search_listings' else ''

        # Tables use full public details: search previews omit rows and must not render as "không có".
        full = {item["id"]: item for item in (table_source if table_source is not None else evidence["listings"])}
        recommend = bool(RECOMMENDATION.search(question))
        wants_table = bool(TABLE_REQUEST.search(question))
        sections = requested_sections(question, search_page=tool == "search_listings")
        show_table = tool == "search_listings" or len(full) >= 1 and (wants_table or len(full) >= 2 and bool(sections or recommend))

        def table_for(selected: list[str]) -> str:
            rows = [full[value] for value in selected if value in full] or [full[value] for value in ids if value in full]
            return listing_table(rows, references, sections, distances) if show_table else ""

        # Every search page (first hit, "xem thêm", refinements) uses the same table. Advice is the only prose path.
        if tool == "search_listings" and not recommend and (
                evidence.get("moreResults") or MORE_PAGE.search(question) or not ADVICE_REQUEST.search(question)):
            return AskResult(answer=self._browse_answer(question, evidence, table_for(ids)), status="ANSWERED",
                             citations=[], request_id=request_id, listing_context=result_context, listing_ids=ids)
        if tool != "search_listings" and not recommend and (wants_table or show_table and sections and not ADVICE_REQUEST.search(question)):
            return AskResult(answer=self._table_answer(sections, len(full), table_for(ids)), status="ANSWERED",
                             citations=[], request_id=request_id, listing_context=result_context, listing_ids=ids)
        parking_answer = None if recommend else self._parking_answer(question, evidence["listings"], references)
        if parking_answer is not None:
            if count_notice:
                parking_answer = count_notice + '\n' + parking_answer
            if show_table:
                parking_answer += "\n\n" + table_for(ids)
            return AskResult(answer=parking_answer, status="ANSWERED", citations=[], request_id=request_id,
                             listing_context=result_context, listing_ids=ids)
        if show_table:
            evidence["tableRendered"] = section_labels(sections) or ["Thông tin chính"]
        try:
            generated = await self.generative_client.generate_listing_answer(question=question, evidence=evidence)
            if any(value not in ids for value in generated.listing_ids):
                raise GenerationUnavailableError("Unknown listing reference.", retryable=False)
            raw = customer_listing_text(generated.answer)
            if show_table:
                raw = strip_tables(raw)
            if evidence["mainRecommendationManaged"]:
                raw = without_managed_choice(raw)
            selected_ids = list(dict.fromkeys(generated.listing_ids))
            if show_table:
                selected_ids = ids  # A follow-up comparison keeps every current column; advice is separate.
            if tool == 'search_listings' and requested_count(question) is not None:
                selected_ids = ids  # Explicit page size applies to cards too, not just model prose references.
            raw = re.sub(r"\[([^\]]+)\]\(/rent/[0-9a-fA-F-]{36}\)", r"\1", raw)
            raw = re.sub(r"(?:https?://[^\s/]+)?/rent/[0-9a-fA-F-]{36}", "", strip_images(raw))
        except GenerationUnavailableError as error:
            self._log_failure(request_id, "answer_generation", error, tool)
            return AskResult(answer="Mình đã kiểm tra tin đăng, nhưng chưa thể tạo câu trả lời trọn vẹn lúc này. "
                                    "Bạn thử lại hoặc hỏi ít tiêu chí hơn nhé.",
                             status="GENERATION_UNAVAILABLE", citations=[], request_id=request_id, listing_context=result_context)
        if show_table:
            raw += "\n\n" + table_for(selected_ids)
        if count_notice:
            raw = count_notice + '\n' + raw
        return AskResult(answer=raw.strip(), status="ANSWERED", citations=[], request_id=request_id,
                         listing_context=result_context, listing_ids=selected_ids)

    # ------------------------------------------------------------------ outcomes

    @staticmethod
    def _pending(kind: str, question: str) -> dict[str, Any]:
        return {"type": kind, "question": question[:600], "fields": [], "candidates": [], "radiusMeters": None, "proposed": None}

    def _ask(self, request_id: str, scope: list[str], state: dict[str, Any], text: str,
             data_sources: list[str] | None = None) -> AskResult:
        noted = criteria_lines(state, self.catalog)
        if noted and state.get("pending", {}) and state["pending"]["type"] in {"LOCATION", "TARGET", "DATE"}:
            text += "\nMình đã ghi nhận: " + "; ".join(noted) + "."
        logger.info("listing_plan_outcome", request_id=request_id, outcome="CLARIFY", pending=(state.get("pending") or {}).get("type"))
        return AskResult(answer=text, status="ANSWERED", citations=[], request_id=request_id,
                         listing_context=self.context(scope, state), data_sources=data_sources or [])

    async def _model_clarification(self, question: str, history: list[dict], request_id: str, scope: list[str], state: dict[str, Any] | None,
                             arguments: dict[str, Any]) -> AskResult:
        kept = deepcopy(state) if state else rs.empty_state()
        missing = arguments.get("missing") or ["OTHER"]
        if self.catalog:
            # Model tool choice never determines whether already-stated facts are remembered.
            update = normalize(question, {"constraints": [], "context": {}, "location": {}}, self.catalog, state, self.today())
            if not update.get("hypothetical"):
                kept = rs.merge(kept, update, question)
            try:
                location = guidance.explicit_location(question, history)
                if location:
                    kept["location"] = location
            except LocationClarification as error:
                kept["pending"] = self._pending("LOCATION", str(error))
                result = self._ask(request_id, scope, kept, str(error))
                result.status = "NEEDS_CLARIFICATION"
                return result
            if getattr(self, "_require_search_basics", True):
                needed = guidance.missing_basics(kept)
                if needed:
                    text = guidance.intake_question(needed)
                    kept["pending"] = {**self._pending("INTAKE", text), "fields": needed[:2]}
                    result = self._ask(request_id, scope, kept, text)
                    result.status = "NEEDS_CLARIFICATION"
                    return result
                if set(missing) <= {"LOCATION", "CATEGORY", "BUDGET", "OCCUPANTS", "CONTEXT"}:
                    return await self._apply(question, history, request_id, kept, update, scope)
        kind = "LOCATION" if "LOCATION" in missing else "TARGET" if "TARGET" in missing else "DATE" if "DATE" in missing else "MODEL"
        text = arguments["question"]
        if arguments.get("options"):
            text += "\n" + "\n".join(f"- {option}" for option in arguments["options"])
        kept["pending"] = self._pending(kind, text)
        return self._ask(request_id, scope, kept, text)

    def _plan_rejected(self, request_id: str, scope: list[str], state: dict[str, Any] | None, reason: str,
                       field: str | None, allowed: list[str]) -> AskResult:
        label = self.catalog.label(field) if self.catalog and field else "một điều kiện"
        if reason in {"NEARBY_UNAVAILABLE"}:
            text = "Dữ liệu tiện ích xung quanh hiện chưa sẵn sàng cho điều kiện này, nên mình không lọc thay bằng tiêu chí khác. Bạn muốn tìm theo khu vực và các điều kiện còn lại không?"
        elif reason in {"TOO_COMPLEX"}:
            text = "Yêu cầu có quá nhiều điều kiện cùng lúc để lọc chính xác. Bạn ưu tiên giữ những điều kiện nào trước?"
        elif reason == "INVALID_VALUE" and allowed and self.catalog and field:
            options = ", ".join(self.catalog.value_label(field, value) for value in allowed[:8])
            text = f"Mình chưa hiểu đúng giá trị cho “{label}”. Các lựa chọn hợp lệ: {options}. Bạn chọn giúp mình nhé?"
        else:
            text = f"Mình chưa chuyển được điều kiện “{label}” thành bộ lọc chính xác, nên chưa tìm để tránh trả kết quả sai. Bạn diễn đạt lại điều kiện này giúp mình nhé?"
        logger.info("listing_plan_outcome", request_id=request_id, outcome="PLAN_REJECTED", reason=reason, field=field)
        return AskResult(answer=text, status="ANSWERED", citations=[], request_id=request_id,
                         listing_context=self.context(scope, state))

    def _no_results(self, request_id: str, persist: dict[str, Any], criteria: dict[str, Any], result: dict[str, Any],
                    *, more: bool = False, changed: int = 0) -> AskResult:
        lines = criteria_lines(criteria, self.catalog)
        if more:
            text = "Mình đã hiển thị hết các tin khớp đủ tiêu chí hiện tại; không còn tin khác để xem thêm."
        else:
            text = "Mình chưa thấy tin đang hiển thị nào khớp đủ các điều kiện bạn đặt"
            text += (":\n" + "\n".join("- " + line for line in lines)) if lines else "."
        if changed:
            text += f"\n{changed} tin vừa tìm thấy đã đổi trạng thái hoặc không còn công khai, nên mình không đưa ra."
        if result.get("unknownTotal"):
            text += (f"\nCó {result['unknownTotal']} tin chưa đủ dữ liệu để xác nhận một số điều kiện (ví dụ chưa khai báo hoặc chưa có dữ liệu xung quanh), "
                     "nên mình không xếp chúng là phù hợp.")
        missing = {"occupants": "số người ở", "motorbikes": "số xe máy", "cars": "số ô tô", "electricityKwh": "lượng điện dùng mỗi tháng",
                   "waterM3": "lượng nước dùng mỗi tháng"}
        if result.get("contextMissing"):
            text += "\nĐể tính đúng chi phí, bạn cho mình biết thêm " + ", ".join(missing.get(m, m) for m in result["contextMissing"]) + "."
        if criteria.get("unsupported"):
            text += "\nMột số mong muốn (như yên tĩnh, an ninh) chưa có dữ liệu để lọc, mình chỉ ghi nhận chứ không dùng để loại tin."
        if not more:
            text += "\nMình không tự nới điều kiện. Bạn muốn đổi điều kiện nào (ví dụ tăng ngân sách, bỏ một tiện ích hay mở rộng khu vực)? Các điều kiện còn lại mình giữ nguyên."
        state = deepcopy(persist)
        state["pending"] = self._pending("EMPTY_RESULT", "Bạn muốn đổi điều kiện nào?")
        logger.info("listing_plan_outcome", request_id=request_id, outcome="NO_RESULTS", unknown=result.get("unknownTotal"))
        return AskResult(answer=text, status="NO_RESULTS", citations=[], request_id=request_id,
                         listing_context=self.context([], state))

    def _clarify(self, request_id: str, scope: list[str], state: dict[str, Any] | None) -> AskResult:
        answer = (f"Bạn đang muốn chọn trong {len(scope)} tin vừa xem, hay tìm thêm tin mới? "
                  "Nếu hỏi một tin cụ thể, bạn cho mình số thứ tự trong danh sách nhé." if scope else
                  "Bạn cho mình biết khu vực muốn tìm (phường/xã, tỉnh/thành hoặc trường/nơi làm việc cụ thể) và nhu cầu chính nhé, để mình kiểm tra đúng tin đăng.")
        return AskResult(answer=answer, status="ANSWERED", citations=[], request_id=request_id,
                         listing_context=self.context(scope, state))

    @staticmethod
    def _log_failure(request_id: str, stage: str, error: Exception, tool: str | None = None, reason: str | None = None) -> None:
        # Do not log prompts, bearer tokens, provider bodies, user history or public listing data.
        logger.warning("listing_request_failed", request_id=request_id, stage=stage,
                       error_type=type(error).__name__, tool=tool, reason=reason,
                       http_status=getattr(error, "status_code", None))

    def _unavailable(self, request_id: str, scope: list[str] | None = None, state: dict[str, Any] | None = None) -> AskResult:
        return AskResult(answer="Mình chưa kiểm tra được tin đăng trực tiếp lúc này, nên không muốn đoán thông tin cho bạn. Bạn thử lại sau ít phút nhé.",
                         status="TOOL_UNAVAILABLE", citations=[], request_id=request_id,
                         listing_context=self.context(scope or [], state) if scope or state else None)

    # ------------------------------------------------------------------ templates

    @staticmethod
    def _is_browse_query(question: str) -> bool:
        return bool(VietnamesePattern(r"\s*(?:tìm|kiếm|cho (?:tôi|mình) xem)\b").match(question)) and not bool(
            VietnamesePattern(r"so sánh|bao nhiêu|cho (?:tôi|mình) biết|chi tiết|đặt cọc|tiền cọc|\bphí\b|"
                              r"nội thất|tiện ích|điều kiện thuê|lịch xem|tính thế nào|nên chọn|tư vấn|gợi ý|phù hợp nhất").search(question))

    @staticmethod
    def _table_answer(sections: list[str], count: int, table: str) -> str:
        if sections:
            intro = "Mình đã đọc đầy đủ " + ", ".join(label.lower() for label in section_labels(sections)) + \
                    (" của các tin đang xét:" if count > 1 else " của tin này:")
        else:
            intro = f"Mình đặt {count} tin cạnh nhau để bạn dễ xem điểm khác biệt:" if count > 1 else "Thông tin chính của tin này:"
        return intro + "\n\n" + table + "\n\n" + TABLE_HINT

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
    def _browse_answer(question: str, evidence: dict[str, Any], table: str = "") -> str:
        listings = evidence["listings"]
        first = listings[0]
        categories = {item.get("category") for item in listings}
        category_name = {"ROOM": "phòng trọ", "HOUSE": "nhà nguyên căn", "APARTMENT": "căn hộ"}.get(
            first.get("category"), "tin đăng") if len(categories) == 1 else "tin đăng"
        wards = {item.get("address", {}).get("ward") for item in listings}
        ward = next(iter(wards)) if len(wards) == 1 else None
        total = evidence.get("total") or len(listings)
        keyword = evidence.get("keyword")
        target = (evidence.get("target") or {}).get("name")
        where = (f" trong bán kính đã chọn quanh {target}" if target else f" có nhắc đến “{keyword}”" if keyword
                 else f" tại {ward}" if ward else "")
        more = bool(evidence.get("moreResults"))
        if more:
            lines = [f"**{len(listings)} {category_name}** tiếp theo:"]
        else:
            shown = f", dưới đây là {len(listings)} tin" if total > len(listings) else ""
            lines = [f"Mình tìm thấy **{total} {category_name}** khớp đủ tiêu chí{where}{shown}."]
            criteria = evidence.get("requestedCriteria") or []
            if criteria:
                lines.append("**Tiêu chí đang áp dụng:** " + " · ".join(criteria))
        notice = ListingAsk._count_notice(evidence)
        if notice and not more:
            lines.append(notice)
        if evidence.get("unknownTotal") and not more:
            lines.append(f"Ngoài ra có {evidence['unknownTotal']} tin chưa đủ dữ liệu để xác nhận một số tiêu chí nên chưa được liệt kê.")
        if evidence.get("unavailableListings"):
            lines.append(f"{evidence['unavailableListings']} tin vừa đổi trạng thái nên không hiển thị.")
        if table:
            lines.append(table)
        remaining = total - (evidence.get("shownSoFar") or len(listings))
        if remaining > 0:
            lines.append(f"Còn **{remaining} tin**. Nói “xem thêm” để xem tiếp.")
        if not more:
            lines.append(TABLE_HINT)
        return "\n\n".join(lines)

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
