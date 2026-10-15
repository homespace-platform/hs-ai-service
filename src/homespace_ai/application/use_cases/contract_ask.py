"""Answer questions about the signed-in account's own contracts from Core read-only projections.

Pipeline: route (application code) -> resolve which contract (stored references / Core search, never the
model) -> bounded read-only tool calls -> answer. Cards and links are built here from verified Core
results; the model writes prose only. Core computes sums, deadlines, renewal eligibility and effective
terms; nothing here recomputes them.
"""

import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from fastapi import Request
import structlog

from homespace_ai.application.contract_context import (
    Route, classify, last_context, make_context, resolve_reference, valid_ids)
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.clients.contract_tool_planner import ContractToolPlanner
from homespace_ai.clients.generative import BaseGenerativeClient, GenerationUnavailableError
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.clients.listing_tool_planner import ToolPlan
from homespace_ai.core.config import Settings
from homespace_ai.tools.contracts import ContractToolError, ContractTools, normalize

logger = structlog.get_logger(__name__)

# Hard limits for one user turn.
MAX_MODEL_ROUNDS = 3
MAX_TOOL_CALLS = 6
TOTAL_TIMEOUT_SECONDS = 40.0
MAX_EVIDENCE_CHARS = 60_000
MAX_CARDS = 5
GENERIC_PERSONAL_FIELDS = ("lines", "changes")  # dropped first when evidence is too large

_VN = timezone(timedelta(hours=7))
RagCallable = Callable[[str], Awaitable[AskResult]]

STATUS_LABELS = {
    "DRAFT": "Bản nháp", "PENDING_REVIEW": "Chờ rà soát", "LANDLORD_SIGNATURE_PENDING": "Chờ chủ nhà ký",
    "TENANT_SIGNATURE_PENDING": "Chờ người thuê ký", "ACTIVE": "Đang có hiệu lực", "EXPIRED": "Đã hết thời hạn",
    "CLOSED": "Đã tất toán", "TERMINATED": "Đã chấm dứt", "CANCELLED": "Đã hủy"}
PHASE_LABELS = {
    "NOT_SIGNED": "Chưa ký đủ", "NOT_STARTED": "Đã ký, chưa đến ngày thuê", "IN_LEASE": "Đang trong thời hạn thuê",
    "LEASE_ENDED": "Đã hết thời hạn thuê", "CLOSED": "Đã tất toán", "TERMINATED": "Đã chấm dứt",
    "CANCELLED": "Đã hủy", "UNKNOWN": "Chưa rõ"}
ROLE_LABELS = {"LANDLORD": "Chủ nhà", "TENANT": "Người thuê"}

CURRENT = re.compile(r"hiện tại|hiện nay|đang thuê|đang cho thuê|lúc này|bây giờ|đang có hiệu lực|đang hiệu lực", re.IGNORECASE)
LANDLORD_ROLE = re.compile(r"cho thuê|chủ nhà là tôi|nhà của tôi|tôi là chủ", re.IGNORECASE)
TENANT_ROLE = re.compile(r"\btôi thuê|đang thuê|mình thuê|người thuê là tôi", re.IGNORECASE)
COUNT_INTENT = re.compile(r"bao nhiêu hợp đồng|mấy hợp đồng|tổng (?:số )?hợp đồng|thống kê", re.IGNORECASE)
EXPIRING_INTENT = re.compile(r"sắp hết hạn|sắp đến hạn|hết hạn trong|sắp kết thúc", re.IGNORECASE)
LIST_INTENT = re.compile(r"có (?:những )?hợp đồng nào|danh sách hợp đồng|liệt kê hợp đồng|hợp đồng nào|các hợp đồng|những hợp đồng", re.IGNORECASE)
AWAITING_SIGN = re.compile(r"chờ (?:tôi )?ký|chưa ký|cần ký|đang chờ ký", re.IGNORECASE)
CONTRACT_NUMBER = re.compile(r"\bHD[-/A-Za-z0-9]{3,40}", re.IGNORECASE)
DATE_TEXT = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?!\d)")
TOPICS: list[tuple[str, re.Pattern[str]]] = [
    ("detail", re.compile(r"đã ký|ký đủ|ký chưa|chữ ký|hiệu lực|trạng thái|chờ ký|ai (?:chưa )?ký|đối tác|địa chỉ|thông tin hợp đồng|tình trạng", re.IGNORECASE)),
    ("terms", re.compile(r"nội thất|trang thiết bị|thiết bị|tiện ích|giá|tiền thuê|phí|điện|nước|điều khoản|chính sách|phạt|số người|\bxe\b|dịch vụ|gửi xe", re.IGNORECASE)),
    ("amendments", re.compile(r"phụ lục|sửa đổi|điều chỉnh|thay đổi", re.IGNORECASE)),
    ("lifecycle", re.compile(r"gia hạn|hết hạn|còn bao lâu|còn mấy ngày|kết thúc|ngày cuối|chấm dứt|trả phòng|thời hạn|bắt đầu|từ ngày", re.IGNORECASE)),
    ("financial", re.compile(r"hóa đơn|hoá đơn|thanh toán|cọc|hoàn|\bnợ\b|đã trả|quá hạn|chuyển khoản|tất toán|quyết toán", re.IGNORECASE)),
]
GROUP_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EQUIPMENT", re.compile(r"nội thất|trang thiết bị|thiết bị", re.IGNORECASE)),
    ("AMENITIES", re.compile(r"tiện ích", re.IGNORECASE)),
    ("PRICING", re.compile(r"giá|tiền thuê", re.IGNORECASE)),
    ("CHARGES", re.compile(r"phí|điện|nước|dịch vụ|gửi xe|\bxe\b", re.IGNORECASE)),
    ("POLICIES", re.compile(r"chính sách|phạt|điều khoản", re.IGNORECASE)),
    ("SPECIAL_TERMS", re.compile(r"điều khoản", re.IGNORECASE)),
    ("OCCUPANCY", re.compile(r"số người", re.IGNORECASE)),
]
RAG_QUERIES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"gia hạn", re.IGNORECASE), "Quy trình gia hạn hợp đồng thuê nhà trên HomeSpace gồm những bước nào và cần điều kiện gì?"),
    (re.compile(r"phụ lục|sửa đổi", re.IGNORECASE), "Quy trình tạo, đề xuất và ký phụ lục sửa đổi hợp đồng trên HomeSpace là gì?"),
    (re.compile(r"chấm dứt|trả phòng|kết thúc", re.IGNORECASE), "Quy trình chấm dứt hợp đồng thuê và tất toán tiền cọc trên HomeSpace như thế nào?"),
    (re.compile(r"hoàn cọc|tiền cọc|cọc", re.IGNORECASE), "Tiền cọc và việc hoàn cọc được xử lý như thế nào trên HomeSpace?"),
    (re.compile(r"ký", re.IGNORECASE), "Cách ký hợp đồng điện tử trên HomeSpace như thế nào?"),
    (re.compile(r"thanh toán|hóa đơn|hoá đơn|tiền thuê", re.IGNORECASE), "Cách thanh toán hóa đơn tiền thuê hằng tháng trên HomeSpace như thế nào?"),
]


class _Budget:
    def __init__(self) -> None:
        self.calls = 0

    def spend(self) -> None:
        if self.calls >= MAX_TOOL_CALLS:
            raise ContractToolError("UNAVAILABLE")  # a bug or an abusive plan, never an unbounded loop
        self.calls += 1

    @property
    def left(self) -> int:
        return MAX_TOOL_CALLS - self.calls


def _fmt_date(value: Any) -> str:
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return f"{value[8:10]}/{value[5:7]}/{value[:4]}"
    return "chưa rõ"


def _fmt_instant(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(_VN).strftime("%H:%M %d/%m/%Y")
    except (ValueError, TypeError):
        return "chưa rõ"


class ContractAsk:
    def __init__(self, settings: Settings, generative_client: BaseGenerativeClient | None,
                 gateway: GatewayApiClient | None, request: Request, *, is_guest: bool = False,
                 rag: RagCallable | None = None, tools: Any = None, planner: Any = None) -> None:
        self.settings = settings
        self.generative_client = generative_client
        self.is_guest = is_guest
        self.rag = rag
        self.tools = tools or ContractTools(gateway, request, is_guest=is_guest)  # type: ignore[arg-type]
        self.planner = planner or ContractToolPlanner(settings)

    # ------------------------------------------------------------------ entry point

    async def try_answer(self, question: str, history: list[dict[str, Any]], request_id: str) -> AskResult | None:
        route = classify(question, history)
        if route is None:
            return None
        context, _ = last_context(history)
        if self.is_guest:
            return self._login_required(request_id)
        try:
            return await asyncio.wait_for(self._run(question, route, context, request_id), TOTAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("contract_request_timeout", request_id=request_id)
            return self._tool_unavailable(request_id, context)
        except ContractToolError as error:
            logger.warning("contract_tool_failed", request_id=request_id, kind=error.kind, http_status=error.status_code)
            if error.kind == "UNAUTHENTICATED":
                return self._login_required(request_id)
            if error.kind == "NOT_FOUND":
                return AskResult(
                    answer="Mình không tìm thấy hợp đồng này trong tài khoản đang đăng nhập của bạn. "
                           "Bạn thử nêu số hợp đồng hoặc hỏi “hợp đồng của tôi gồm những gì” để mình liệt kê lại nhé.",
                    status="NOT_FOUND", citations=[], request_id=request_id, contract_context=context,
                    data_sources=["CORE_CONTRACT"])
            return self._tool_unavailable(request_id, context)

    # ------------------------------------------------------------------ orchestration

    async def _run(self, question: str, route: Route, context: dict[str, Any] | None, request_id: str) -> AskResult:
        budget = _Budget()
        ref_id, out_of_range = resolve_reference(question, context)
        if out_of_range:
            return self._clarify_range(request_id, context)

        if ref_id is None:
            aggregate = await self._aggregate(question, budget, request_id)
            if aggregate is not None:
                return aggregate
            ref_id, candidate_result = await self._pick_contract(question, budget, request_id)
            if candidate_result is not None:
                return candidate_result
        assert ref_id is not None

        results: dict[str, dict[str, Any]] = {}
        requested = self._topic_plans(question, ref_id)
        if not requested and self._can_plan():
            requested = await self._plan_with_model(question, ref_id, budget, results)
        if not requested and not results:
            requested = [ToolPlan("get_contract_lifecycle", {"contractId": ref_id})]
        # detail always accompanies the answer: it supplies the verified card and the signing/lease state.
        if not any(p.name == "get_contract_detail" for p in requested) and "get_contract_detail" not in results:
            requested.insert(0, ToolPlan("get_contract_detail", {"contractId": ref_id}))
        for plan in requested[:4]:
            if plan.name in results:
                continue
            budget.spend()
            results[plan.name] = await self.tools.call(plan.name, plan.arguments)

        detail = results.get("get_contract_detail") or next(iter(results.values()))
        card = self._card(detail)
        cards = [card] if card else []
        business_date = (detail.get("meta") or {}).get("businessDate")
        new_context = make_context([card["contractId"]] if card else [ref_id],
                                   [card["contractNumber"]] if card else None,
                                   ref_id, card.get("askerRole") if card else None, business_date)

        evidence = {"meta": detail.get("meta"), "contract": {k: detail.get(k) for k in ("contractNumber", "askerRole")},
                    "results": results}
        try:
            core_answer = await self._generate(question, evidence)
        except GenerationUnavailableError as error:
            logger.warning("contract_generation_failed", request_id=request_id, error_type=type(error).__name__)
            return AskResult(
                answer="Mình đã đọc được hợp đồng của bạn nhưng chưa thể tạo câu trả lời trọn vẹn lúc này. "
                       "Bạn thử lại sau ít phút, hoặc mở thẻ hợp đồng bên dưới để xem trực tiếp nhé.",
                status="GENERATION_UNAVAILABLE", citations=[], request_id=request_id,
                contract_context=new_context, contract_cards=cards, data_sources=["CORE_CONTRACT"])

        if route.kind == "CONTRACT_AND_RAG" and self.rag is not None:
            return await self._combine(question, core_answer, evidence, cards, new_context, request_id)
        return AskResult(answer=core_answer, status="ANSWERED", citations=[], request_id=request_id,
                         contract_context=new_context, contract_cards=cards, data_sources=["CORE_CONTRACT"])

    # ------------------------------------------------------------------ aggregate questions

    async def _aggregate(self, question: str, budget: _Budget, request_id: str) -> AskResult | None:
        role = self._role(question)
        if COUNT_INTENT.search(question):
            budget.spend()
            summary = await self.tools.call("summarize_my_contracts", {k: v for k, v in {"role": role}.items() if v})
            expiring = (summary.get("expiring") or {}).get("contracts") or []
            cards = [c for c in (self._card(item) for item in expiring[:MAX_CARDS]) if c]
            return AskResult(
                answer=self._summary_text(summary), status="ANSWERED", citations=[], request_id=request_id,
                contract_context=self._list_context(cards, role, summary), contract_cards=cards,
                data_sources=["CORE_CONTRACT"])
        wants_expiring = bool(EXPIRING_INTENT.search(question))
        awaiting = bool(AWAITING_SIGN.search(question))
        if wants_expiring or awaiting or LIST_INTENT.search(question):
            args: dict[str, Any] = {"size": 5}
            if role:
                args["role"] = role
            if wants_expiring:
                days = re.search(r"(\d{1,3})\s*ngày", question)
                args.update({"expiringWithinDays": min(max(int(days.group(1)), 1), 365) if days else 30,
                             "sort": "LEASE_END_ASC"})
            if awaiting:
                args["status"] = ["LANDLORD_SIGNATURE_PENDING", "TENANT_SIGNATURE_PENDING"]
            budget.spend()
            found = await self.tools.call("search_my_contracts", args)
            return self._list_answer(found, request_id, wants_expiring, role)
        return None

    async def _pick_contract(self, question: str, budget: _Budget, request_id: str) -> tuple[str | None, AskResult | None]:
        """Find the one contract the user means; ask them to choose when it is not unique."""
        args: dict[str, Any] = {"size": 6}
        role = self._role(question)
        number = CONTRACT_NUMBER.search(question)
        if role:
            args["role"] = role
        if number:
            args["q"] = number.group(0)
        current = bool(CURRENT.search(question)) and not number
        if current:
            args.update({"status": ["ACTIVE"], "leasePhase": "IN_LEASE"})
        budget.spend()
        found = await self.tools.call("search_my_contracts", args)
        relaxed = False
        if not found.get("items") and current:
            loosened = {k: v for k, v in args.items() if k not in {"status", "leasePhase"}}
            budget.spend()
            found = await self.tools.call("search_my_contracts", loosened)
            relaxed = True
        items = found.get("items") or []
        if not items:
            return None, AskResult(
                answer="Theo dữ liệu hệ thống vừa đọc, tài khoản của bạn chưa có hợp đồng nào khớp với yêu cầu này.",
                status="NO_RESULTS", citations=[], request_id=request_id,
                contract_context=make_context([]), data_sources=["CORE_CONTRACT"])
        if len(items) == 1 and not relaxed and found.get("total") == 1:
            return items[0]["contractId"], None
        if relaxed:
            return None, self._list_answer(found, request_id, False, role,
                                           lead="Hiện bạn không có hợp đồng nào đang trong thời hạn thuê và có hiệu lực. "
                                                "Đây là các hợp đồng gần đây của bạn:")
        return None, self._list_answer(found, request_id, False, role,
                                       lead="Bạn đang có nhiều hợp đồng. Bạn muốn hỏi hợp đồng nào "
                                            "(trả lời “hợp đồng thứ 1/2/…” hoặc nêu số hợp đồng)?", clarification=True)

    # ------------------------------------------------------------------ planning

    @staticmethod
    def _topic_plans(question: str, contract_id: str) -> list[ToolPlan]:
        plans: list[ToolPlan] = []
        for name, pattern in TOPICS:
            if not pattern.search(question):
                continue
            if name == "detail":
                plans.append(ToolPlan("get_contract_detail", {"contractId": contract_id}))
            elif name == "terms":
                args: dict[str, Any] = {"contractId": contract_id}
                groups = [group for group, p in GROUP_PATTERNS if p.search(question)]
                if "SPECIAL_TERMS" in groups and "POLICIES" in groups and not re.search(r"chính sách|phạt", question, re.IGNORECASE):
                    groups.remove("POLICIES")
                args["groups"] = groups or ["LEASE", "PRICING", "CHARGES"]
                as_of = DATE_TEXT.search(question)
                if as_of:
                    try:
                        args["asOf"] = date(int(as_of.group(3)), int(as_of.group(2)), int(as_of.group(1))).isoformat()
                    except ValueError:
                        pass  # not a real date: ignore rather than guess
                plans.append(ToolPlan("get_contract_terms", args))
            elif name == "amendments":
                plans.append(ToolPlan("get_contract_amendments", {"contractId": contract_id}))
            elif name == "lifecycle":
                plans.append(ToolPlan("get_contract_lifecycle", {"contractId": contract_id}))
            elif name == "financial":
                plans.append(ToolPlan("get_contract_financial_summary", {"contractId": contract_id}))
        validated = []
        for plan in plans[:4]:
            validated.append(ToolPlan(plan.name, normalize(plan.name, plan.arguments)))
        return validated

    def _can_plan(self) -> bool:
        return self.generative_client is not None and self.settings.generation_provider == "openai"

    async def _plan_with_model(self, question: str, contract_id: str, budget: _Budget,
                               results: dict[str, dict[str, Any]]) -> list[ToolPlan]:
        """Bounded loop for questions the keyword rules cannot place. Only per-contract tools on the known id."""
        chosen: list[ToolPlan] = []
        done: list[dict[str, Any]] = []
        for _ in range(MAX_MODEL_ROUNDS):
            try:
                plans = await self.planner.plan_round(question, None, [contract_id], done, budget.left - len(chosen))
            except GenerationUnavailableError as error:
                logger.warning("contract_planner_failed", error_type=type(error).__name__)
                break
            fresh = [p for p in plans if p.name not in {c.name for c in chosen}
                     and p.name not in {"summarize_my_contracts", "search_my_contracts"}
                     and p.arguments.get("contractId") == contract_id]
            if not fresh:
                break
            chosen.extend(fresh)
            done.extend({"tool": p.name} for p in fresh)  # names only: contract text never reaches the planner
            if len(chosen) >= 3:
                break
        return chosen

    # ------------------------------------------------------------------ answers

    async def _generate(self, question: str, evidence: dict[str, Any]) -> str:
        if self.generative_client is None:
            raise GenerationUnavailableError("No generative client.", retryable=False)
        evidence = self._with_local_times(evidence)
        text = json.dumps(evidence, ensure_ascii=False, default=str)
        if len(text) > MAX_EVIDENCE_CHARS:
            evidence = self._shrink(evidence)
        answer = await self.generative_client.generate_contract_answer(question=question, evidence=evidence)
        # Links come from the application only: strip anything the model might still have produced.
        answer = re.sub(r"\[([^\]]+)\]\((?:https?://|/)[^)]*\)", r"\1", answer)
        answer = re.sub(r"https?://\S+", "", answer)
        answer = re.sub(r"(?<!\S)/[A-Za-z0-9_-]+(?:/[^\s]*)+", "", answer)  # relative app routes
        answer = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "", answer)
        return self._humanize(answer)

    @staticmethod
    def _with_local_times(evidence: Any) -> Any:
        """Core timestamps are UTC instants; give the model ready-made Vietnam local text so it never converts."""
        if not isinstance(evidence, dict):
            return evidence

        def fix(meta: Any) -> Any:
            if not isinstance(meta, dict):
                return meta
            meta = dict(meta)
            for key in ("readAt", "businessNow"):
                if meta.get(key):
                    meta[key + "Local"] = _fmt_instant(meta[key]) + " (giờ Việt Nam)"
                    meta.pop(key, None)  # hide the UTC value so it cannot be quoted
            return meta

        out = {k: (fix(v) if k == "meta" else v) for k, v in evidence.items()}
        for k, v in out.items():
            if isinstance(v, dict) and "meta" in v:
                out[k] = {**v, "meta": fix(v["meta"])}
        return out

    _ENUM_WORDS = {
        "DRAFT": "bản nháp", "NOT_SIGNED": "chưa ký", "ACTIVE": "đang hiệu lực",
        "TRANSFER_REPORTED": "đã báo chuyển khoản", "SIGNED_PENDING_EFFECTIVE": "đã ký, chưa tới ngày hiệu lực",
        "SUPERSEDED": "đã bị thay thế", "REJECTED": "đã bị từ chối", "WITHDRAWN": "đã rút", "EXPIRED": "đã hết hạn",
    }

    @classmethod
    def _humanize(cls, answer: str) -> str:
        """Last line of defence: raw enum/field names must not reach users even if the model slips."""
        # "bản nháp (DRAFT)" -> "bản nháp"; "(fullySigned = false)" -> removed
        answer = re.sub(r"\s*\(\s*[A-Z][A-Z_]{2,}\s*\)", "", answer)
        answer = re.sub(r"\s*\(\s*[a-z]+[A-Z]\w*\s*(?:=|:)\s*(?:true|false|null)\s*\)", "", answer)
        answer = re.sub(r"[^\n]*\b[a-z]+[A-Z]\w*\s*=\s*(?:true|false|null)\b[^\n]*\n?", "", answer)
        answer = re.sub(r"\b[A-Z]{2,}(?:_[A-Z]+)+\b", lambda m: cls._ENUM_WORDS.get(m.group(0), m.group(0).replace("_", " ").lower()), answer)
        answer = re.sub(r"\b(DRAFT|ACTIVE|REJECTED|WITHDRAWN|EXPIRED|SUPERSEDED)\b", lambda m: cls._ENUM_WORDS[m.group(0)], answer)
        return answer.strip()

    @classmethod
    def _shrink(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: cls._shrink(v) for k, v in value.items() if k not in GENERIC_PERSONAL_FIELDS}
        if isinstance(value, list):
            return [cls._shrink(v) for v in value[:20]]
        return value

    async def _combine(self, question: str, core_answer: str, evidence: dict[str, Any], cards: list[dict[str, Any]],
                       context: dict[str, Any], request_id: str) -> AskResult:
        """Personal facts and general guidance stay in separate, labelled sections with separate sources."""
        general_question = next((text for pattern, text in RAG_QUERIES if pattern.search(question)),
                                "Hướng dẫn chung về hợp đồng thuê nhà trên HomeSpace.")
        read_at = _fmt_instant((evidence.get("meta") or {}).get("readAt"))
        try:
            guidance = await self.rag(general_question)  # type: ignore[misc]
        except Exception as error:  # a failed guide must not erase the personal answer
            logger.warning("contract_rag_failed", request_id=request_id, error_type=type(error).__name__)
            guidance = None
        usable = guidance is not None and guidance.status == "ANSWERED" and bool(guidance.citations)
        general = (guidance.answer.strip() if usable else
                   "Hiện tài liệu HomeSpace chưa có hướng dẫn chung phù hợp cho nội dung này.")
        text = (f"**Thông tin hợp đồng của bạn** (đọc từ hệ thống lúc {read_at})\n\n{core_answer}\n\n"
                f"**Hướng dẫn chung từ tài liệu HomeSpace** (không phải dữ liệu riêng của bạn)\n\n{general}")
        return AskResult(answer=text, status="ANSWERED", citations=guidance.citations if usable else [],
                         request_id=request_id, contract_context=context, contract_cards=cards,
                         data_sources=["CORE_CONTRACT", "RAG"] if usable else ["CORE_CONTRACT"])

    def _summary_text(self, summary: dict[str, Any]) -> str:
        meta = summary.get("meta") or {}
        by_role = summary.get("byRole") or {}
        awaiting = summary.get("awaitingSignature") or {}
        expiring = summary.get("expiring") or {}
        simulated = " (ngày nghiệp vụ mô phỏng)" if meta.get("simulatedBusinessClock") else ""
        lines = [f"Theo dữ liệu hệ thống đọc lúc {_fmt_instant(meta.get('readAt'))}, ngày nghiệp vụ "
                 f"{_fmt_date(meta.get('businessDate'))}{simulated}:", ""]
        total = summary.get("total", 0)
        lines.append(f"- **Tổng số hợp đồng:** {total}"
                     + (f" (chủ nhà: {by_role.get('LANDLORD', 0)}, người thuê: {by_role.get('TENANT', 0)})" if total else ""))
        lines.append(f"- **Đang có hiệu lực và trong thời hạn thuê:** {summary.get('inForce', 0)}")
        lines.append(f"- **Đã ký nhưng chưa đến ngày bắt đầu thuê:** {summary.get('signedNotStarted', 0)}")
        lines.append(f"- **Hết thời hạn thuê nhưng chưa tất toán:** {summary.get('leaseEndedNotClosed', 0)}")
        lines.append(f"- **Đang chờ ký:** {awaiting.get('fromAsker', 0)} chờ bạn, {awaiting.get('fromCounterparty', 0)} chờ bên kia")
        lines.append(f"- **Sắp hết hạn trong {expiring.get('withinDays', 30)} ngày:** {expiring.get('count', 0)}")
        lines.append("")
        lines.append("Ngày kết thúc thuê không bao gồm: ngày thuê cuối cùng là ngày liền trước đó.")
        if summary.get("complete") is False:
            lines.append("Lưu ý: hệ thống chỉ thống kê được phần đầu của danh sách, nên số liệu có thể chưa đủ.")
        return "\n".join(lines)

    def _list_answer(self, found: dict[str, Any], request_id: str, expiring: bool, role: str | None,
                     lead: str | None = None, clarification: bool = False) -> AskResult:
        items = found.get("items") or []
        cards = [c for c in (self._card(item) for item in items[:MAX_CARDS]) if c]
        total = found.get("total", len(items))
        meta = found.get("meta") or {}
        if not items:
            text = ("Theo dữ liệu hệ thống vừa đọc, bạn không có hợp đồng nào sắp hết hạn trong thời gian này." if expiring
                    else "Theo dữ liệu hệ thống vừa đọc, không có hợp đồng nào khớp yêu cầu của bạn.")
            return AskResult(answer=text, status="NO_RESULTS", citations=[], request_id=request_id,
                             contract_context=make_context([]), data_sources=["CORE_CONTRACT"])
        lines = [lead or f"Theo dữ liệu hệ thống đọc lúc {_fmt_instant(meta.get('readAt'))}, có {total} hợp đồng khớp yêu cầu:"]
        lines.append("")
        for index, card in enumerate(cards, 1):
            parts = [f"**Hợp đồng {index}:** {card['contractNumber']}", ROLE_LABELS.get(card["askerRole"], ""),
                     card.get("phaseLabel", "")]
            if card.get("leaseEndExclusive"):
                parts.append(f"hết thời hạn thuê trước ngày {_fmt_date(card['leaseEndExclusive'])}")
            lines.append("- " + "; ".join(part for part in parts if part))
        if total > len(cards):
            lines.append(f"\nCòn {total - len(cards)} hợp đồng khác; bạn nêu số hợp đồng hoặc điều kiện lọc để mình thu hẹp nhé.")
        return AskResult(answer="\n".join(lines), status="NEEDS_CLARIFICATION" if clarification else "ANSWERED",
                         citations=[], request_id=request_id, contract_cards=cards,
                         contract_context=self._list_context(cards, role, found), data_sources=["CORE_CONTRACT"])

    def _clarify_range(self, request_id: str, context: dict[str, Any] | None) -> AskResult:
        count = len(context["contractIds"]) if context else 0
        return AskResult(
            answer=f"Danh sách vừa rồi chỉ có {count} hợp đồng. Bạn cho mình số thứ tự đúng trong danh sách "
                   "hoặc số hợp đồng nhé; mình sẽ đọc lại dữ liệu mới nhất.",
            status="NEEDS_CLARIFICATION", citations=[], request_id=request_id, contract_context=context,
            data_sources=[])

    @staticmethod
    def _login_required(request_id: str) -> AskResult:
        return AskResult(
            answer="Để xem hợp đồng của bạn, bạn cần đăng nhập HomeSpace: mình chỉ đọc được hợp đồng thuộc tài khoản đang "
                   "đăng nhập. Nếu muốn biết quy trình chung (ví dụ cách ký hay gia hạn), bạn cứ hỏi không kèm thông tin cá nhân nhé.",
            status="LOGIN_REQUIRED", citations=[], request_id=request_id)

    @staticmethod
    def _tool_unavailable(request_id: str, context: dict[str, Any] | None) -> AskResult:
        return AskResult(
            answer="Mình chưa đọc được dữ liệu hợp đồng của bạn lúc này, nên không muốn đoán thông tin. "
                   "Bạn thử lại sau ít phút nhé.",
            status="TOOL_UNAVAILABLE", citations=[], request_id=request_id, contract_context=context)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _role(question: str) -> str | None:
        if LANDLORD_ROLE.search(question):
            return "LANDLORD"
        if TENANT_ROLE.search(question):
            return "TENANT"
        return None

    @staticmethod
    def _card(source: dict[str, Any]) -> dict[str, Any] | None:
        """Build a card only from a verified Core projection; ids that are not UUIDs produce no card."""
        ids = valid_ids([source.get("contractId")])
        if not ids or not source.get("contractNumber"):
            return None
        lease = source.get("lease") if isinstance(source.get("lease"), dict) else {}
        prop = source.get("property")
        address = prop.get("fullAddress") if isinstance(prop, dict) else prop if isinstance(prop, str) else None
        role = source.get("askerRole") if source.get("askerRole") in ROLE_LABELS else None
        if role is None:
            return None
        status = str(source.get("status") or "")
        phase = str(source.get("leasePhase") or "")
        return {
            "contractId": ids[0], "contractNumber": str(source["contractNumber"])[:60],
            "property": str(address)[:200] if address else None, "askerRole": role,
            "roleLabel": ROLE_LABELS[role], "status": status, "statusLabel": STATUS_LABELS.get(status, status),
            "leasePhase": phase, "phaseLabel": PHASE_LABELS.get(phase, phase),
            "leaseStart": source.get("leaseStart") or lease.get("start"),
            "leaseEndExclusive": source.get("leaseEndExclusive") or lease.get("endExclusive"),
            "lastRentalDay": source.get("lastRentalDay") or lease.get("lastRentalDay"),
            "readAt": (source.get("meta") or {}).get("readAt"), "source": "CORE",
        }

    @staticmethod
    def _list_context(cards: list[dict[str, Any]], role: str | None, source: dict[str, Any]) -> dict[str, Any]:
        meta = source.get("meta") or {}
        return make_context([c["contractId"] for c in cards], [c["contractNumber"] for c in cards], None,
                            role, meta.get("businessDate"))
