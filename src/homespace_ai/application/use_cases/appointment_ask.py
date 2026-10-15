"""Answer questions about the signed-in account's own viewing appointments, and about a listing's viewing
availability, from Core read-only projections.

Pipeline: route (application code) -> resolve which appointment / listing (stored references or Core search,
never the model) -> bounded read-only tool calls -> deterministic Vietnamese answer. Counts, dates, statuses and
which actions are possible come from Core; nothing here recomputes them. Cards and links are built by application
code from verified Core results. The assistant never creates, approves, rejects, changes, cancels or completes an
appointment: action requests are answered with what the screens allow and where to click.

Notes, guest names and reasons are untrusted text: they are only quoted (sanitised and truncated) and never reach a
model prompt or the tool planner.
"""

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import structlog
from fastapi import Request

from homespace_ai.application import appointment_context as ctx
from homespace_ai.application.appointment_context import (
    Reference, Route, classify, fold, last_context, listing_scope, make_context, resolve_listing,
    resolve_reference, valid_ids)
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.clients.appointment_tool_planner import AppointmentToolPlanner
from homespace_ai.clients.generative import BaseGenerativeClient, GenerationUnavailableError
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.clients.listing_tool_planner import ToolPlan
from homespace_ai.core.config import Settings
from homespace_ai.tools.appointments import AppointmentToolError, AppointmentTools, normalize

logger = structlog.get_logger(__name__)

# Hard limits for one user turn.
MAX_MODEL_ROUNDS = 3
MAX_TOOL_CALLS = 6
TOTAL_TIMEOUT_SECONDS = 40.0
PAGE_SIZE = 5
MAX_CARDS = 5
MAX_QUOTE = 200

_VN = timezone(timedelta(hours=7))
RagCallable = Callable[[str], Awaitable[AskResult]]

SOURCE = "CORE_APPOINTMENT"
ROLE_LABELS = {"OWNER": "Chủ nhà", "RENTER": "Người đi xem"}
STATUS_LABELS = {"PENDING": "Đang chờ xác nhận", "CONFIRMED": "Đã xác nhận", "REJECTED": "Đã bị từ chối",
                 "CANCELLED": "Đã hủy", "COMPLETED": "Đã hoàn thành", "EXPIRED": "Đã hết hạn"}
PHASE_LABELS = {"UPCOMING": "Sắp diễn ra", "IN_PROGRESS": "Đang diễn ra", "PAST": "Đã qua giờ hẹn"}
WEEKDAYS_VI = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật"]
WEEKDAY_ENUM = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
ACTIVE = ["PENDING", "CONFIRMED"]
LIVE_AND_DONE = ["PENDING", "CONFIRMED", "COMPLETED"]

ACTION_LABELS = {
    "CANCEL": "hủy lịch", "APPROVE": "chấp nhận yêu cầu", "REJECT": "từ chối yêu cầu",
    "COMPLETE": "đánh dấu hoàn thành", "REQUEST_RESCHEDULE": "đề xuất đổi giờ",
    "APPROVE_RESCHEDULE": "chấp nhận đề xuất đổi giờ", "REJECT_RESCHEDULE": "từ chối đề xuất đổi giờ",
    "BOOK": "đặt lịch xem nhà"}
OWNER_ACTIONS = {"APPROVE", "REJECT", "APPROVE_RESCHEDULE", "REJECT_RESCHEDULE", "CANCEL", "COMPLETE"}
RENTER_ACTIONS = {"REQUEST_RESCHEDULE", "CANCEL"}
BLOCK_TEXT = {
    "STATUS_NOT_PENDING_OR_CONFIRMED": "lịch này không còn ở trạng thái chờ xác nhận hay đã xác nhận",
    "STATUS_NOT_PENDING": "yêu cầu này không còn ở trạng thái chờ xác nhận",
    "STATUS_NOT_CONFIRMED": "chỉ đánh dấu hoàn thành được lịch đã xác nhận",
    "RESCHEDULE_ALREADY_REQUESTED": "đã có một đề xuất đổi giờ đang chờ chủ nhà quyết định",
    "LISTING_NOT_PUBLISHED": "tin đăng hiện không còn được hiển thị",
    "NO_PENDING_RESCHEDULE_REQUEST": "hiện không có đề xuất đổi giờ nào đang chờ",
}
SLOT_STATE_ORDER = ["CAN_REQUEST", "YOUR_REQUEST_PENDING", "YOUR_BOOKING_CONFIRMED", "OWNER_CANNOT_BOOK_OWN_LISTING",
                    "NOT_AVAILABLE"]

COUNT_INTENT = re.compile(r"\bbao nhieu\b|\bmay (?:lich|buoi|yeu cau|khach|nguoi)\b|\btong so\b|\bthong ke\b|\bdem\b")
NEXT_INTENT = re.compile(r"\btiep theo\b|\bgan nhat\b|\bke tiep\b|\bsap toi nhat\b|\blich dau tien\b")
UPCOMING_WORDS = re.compile(r"\bsap toi\b|\bsap den\b|\bsap dien ra\b|\bchua toi\b|\bsap xem\b|\bnhung lich con lai\b")
PAST_WORDS = re.compile(r"\bda qua\b|\bda xem\b|\bxem roi\b|\btruoc do\b|\bvua roi\b|\blich cu\b|\bhom qua\b|\bqua khu\b")
LIVE_WORDS = re.compile(r"\bdang dien ra\b|\bdang xem\b|\bdang trong gio\b")
RESCHEDULE_WORDS = re.compile(r"\bdoi (?:lich|gio|thoi gian)\b|\bgio moi\b|\bde xuat (?:doi|gio|lich)\b|\bxin doi\b")
OWNER_ROLE = re.compile(
    r"\bkhach (?:dat|hen|xin|yeu cau|dang ky|xem|nao|da|co|chua)\b|\bnguoi thue (?:dat|hen|xin|yeu cau|nao|da|co|chua)\b|"
    r"\bnha cua toi\b|\btin (?:dang )?cua toi\b|\btoi la chu\b|\blich tiep khach\b|\bai (?:dat|hen|xin|yeu cau)\b|"
    r"\bcan toi (?:duyet|xac nhan)\b|\bcho toi (?:duyet|xac nhan)\b|\bnguoi (?:dang ky|den) xem nha\b|\bkhach den xem\b")
RENTER_ROLE = re.compile(
    r"\btoi (?:da |dang |se |muon )?(?:dat|dang ky|xin|hen|di xem|yeu cau)\b|\bminh (?:da |dang |se )?(?:dat|dang ky|xin|hen|di xem)\b|"
    r"\bdi xem\b|\blich (?:toi|minh) dat\b|\bchu nha (?:da |chua )?(?:duyet|xac nhan|phan hoi|dong y|chap nhan|tu choi)\b|\bdang cho chu nha\b")
STATUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PENDING", re.compile(r"\bcho (?:chu nha |toi |duyet |xac nhan |phan hoi)|\bchua (?:duoc )?(?:duyet|xac nhan|phan hoi|chap nhan)\b|\bpending\b|\bdang cho\b")),
    ("CONFIRMED", re.compile(r"\bda (?:duoc )?(?:xac nhan|duyet|chap nhan)\b|\bxac nhan roi\b|\bduoc duyet\b")),
    ("REJECTED", re.compile(r"\bbi tu choi\b|\bda tu choi\b")),
    ("CANCELLED", re.compile(r"\bda huy\b|\bbi huy\b|\blich huy\b")),
    ("COMPLETED", re.compile(r"\bda hoan thanh\b|\bxem xong\b|\bhoan thanh\b")),
    ("EXPIRED", re.compile(r"\bhet han\b|\bqua han\b|\bbi het\b")),
]
TOPIC_NOTES = re.compile(r"\bghi chu\b|\bloi nhan\b|\bnote\b")
TOPIC_WHO = re.compile(r"\bai dat\b|\bai xin\b|\bkhach nao\b|\bnguoi nao\b|\bten khach\b|\bnguoi thue nao\b|\bai la\b|\bkhach la ai\b")
TOPIC_ACTIONS = re.compile(r"\bco the\b|\bduoc khong\b|\blam gi\b|\bthao tac\b|\bdoi\b|\bhuy\b|\bduyet\b|\btu choi\b")
TOPIC_HISTORY = re.compile(r"\blich su\b|\bdoi truoc\b|\btruoc do\b|\blan doi\b")
TOPIC_CONTACT = re.compile(r"\bso dien thoai\b|\bsdt\b|\bemail\b|\bzalo\b|\blien he\b")
TOPIC_PEOPLE = re.compile(r"\bbao nhieu nguoi\b|\bso nguoi\b|\bmay nguoi\b|\bdong nguoi\b")


class _Budget:
    def __init__(self) -> None:
        self.calls = 0

    def spend(self) -> None:
        if self.calls >= MAX_TOOL_CALLS:
            raise AppointmentToolError("UNAVAILABLE")  # a bug or an abusive plan, never an unbounded loop
        self.calls += 1

    @property
    def left(self) -> int:
        return MAX_TOOL_CALLS - self.calls


# ---------------------------------------------------------------- formatting helpers

def _fmt_date(value: Any) -> str:
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return f"{value[8:10]}/{value[5:7]}/{value[:4]}"
    return "chưa rõ"


def _weekday(value: Any) -> str:
    try:
        return WEEKDAYS_VI[date.fromisoformat(str(value)[:10]).weekday()]
    except ValueError:
        return ""


def _hm(value: Any) -> str:
    return str(value)[:5] if isinstance(value, str) and re.match(r"^\d{2}:\d{2}", value) else "?"


def _fmt_instant(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(_VN).strftime("%H:%M %d/%m/%Y") + " (giờ Việt Nam)"
    except (ValueError, TypeError):
        return "thời điểm vừa rồi"


def _sched(schedule: Any) -> str:
    if not isinstance(schedule, dict) or not schedule.get("date"):
        return "chưa rõ"
    return (f"{_weekday(schedule['date'])}, {_fmt_date(schedule['date'])}, "
            f"{_hm(schedule.get('startTime'))}–{_hm(schedule.get('endTime'))}")


def _clean(value: Any, limit: int = MAX_QUOTE) -> str:
    """Untrusted user text: remove markup, links and control characters, then truncate."""
    text = re.sub(r"https?://\S+|www\.\S+", "(liên kết đã ẩn)", str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f`*_#<>\[\]{}|~]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _state_text(item: dict[str, Any]) -> str:
    """One natural phrase combining the stored status with the time-derived phase. Never invents attendance."""
    owner = item.get("requesterRole") == "OWNER"
    status, phase = item.get("status"), item.get("temporalPhase")
    if status == "PENDING":
        who = "bạn" if owner else "chủ nhà"
        if phase == "PAST":
            return "yêu cầu đã qua giờ hẹn mà chưa được xác nhận (hệ thống chưa cập nhật trạng thái)"
        if phase == "IN_PROGRESS":
            return f"yêu cầu vẫn đang chờ {who} xác nhận dù đã đến giờ hẹn"
        return f"yêu cầu đang chờ {who} xác nhận"
    if status == "CONFIRMED":
        if phase == "PAST":
            return "đã xác nhận, đã qua giờ hẹn (hệ thống chưa cập nhật sang đã hoàn thành)"
        return "đã xác nhận, đang trong giờ hẹn" if phase == "IN_PROGRESS" else "đã xác nhận, sắp diễn ra"
    return {"REJECTED": "yêu cầu đã bị từ chối", "CANCELLED": "đã hủy", "COMPLETED": "đã hoàn thành",
            "EXPIRED": "đã hết hạn (qua thời gian hẹn mà không có quyết định)"}.get(str(status), "chưa rõ trạng thái")


def _proposed_text(item: dict[str, Any]) -> str | None:
    proposed = item.get("proposedSchedule")
    if proposed:
        return (f"đề xuất đổi sang {_sched(proposed)} (chưa được duyệt; lịch hiện tại vẫn là "
                f"{_sched(item.get('currentSchedule'))})")
    if item.get("rescheduleRequested"):
        return "đang có đề xuất đổi giờ chờ xử lý"
    return None


def _listing_text(listing: Any) -> str:
    if not isinstance(listing, dict):
        return "tin đăng không rõ"
    title = _clean(listing.get("title"), 80) or "Tin đăng"
    address = _clean(listing.get("address"), 100)
    return f"{title} ({address})" if address else title


# ---------------------------------------------------------------- the orchestrator

class AppointmentAsk:
    def __init__(self, settings: Settings, generative_client: BaseGenerativeClient | None,
                 gateway: GatewayApiClient | None, request: Request, *, is_guest: bool = False,
                 rag: RagCallable | None = None, tools: Any = None, planner: Any = None,
                 today: Callable[[], date] | None = None) -> None:
        self.settings = settings
        self.generative_client = generative_client
        self.is_guest = is_guest
        self.rag = rag
        self.tools = tools or AppointmentTools(gateway, request, is_guest=is_guest)  # type: ignore[arg-type]
        self.planner = planner or AppointmentToolPlanner(settings)
        self.today = today or (lambda: datetime.now(_VN).date())  # real Vietnam clock, never billing time

    # ------------------------------------------------------------------ entry point

    async def try_answer(self, question: str, history: list[dict[str, Any]], request_id: str) -> AskResult | None:
        route = classify(question, history)
        if route is None:
            return None
        context, _ = last_context(history)
        scope = listing_scope(history)
        if self.is_guest:
            return self._login_required(request_id)
        try:
            return await asyncio.wait_for(self._run(question, route, context, scope, request_id),
                                          TOTAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("appointment_request_timeout", request_id=request_id)
            return self._tool_unavailable(request_id, context, scope)
        except AppointmentToolError as error:
            logger.warning("appointment_tool_failed", request_id=request_id, kind=error.kind,
                           http_status=error.status_code)
            if error.kind == "UNAUTHENTICATED":
                return self._login_required(request_id)
            if error.kind == "NOT_FOUND":
                return self._result(
                    "Mình không tìm thấy lịch hẹn hoặc tin đăng này trong dữ liệu mà tài khoản của bạn được xem. "
                    "Bạn thử hỏi “lịch hẹn của tôi hôm nay” để mình liệt kê lại nhé.",
                    "NOT_FOUND", request_id, context=context, scope=scope)
            if error.kind == "INVALID":
                return self._result(
                    "Mình chưa hiểu rõ khoảng thời gian hoặc điều kiện lọc bạn muốn (ví dụ giới hạn tối đa khoảng một năm). "
                    "Bạn nói lại cụ thể hơn, chẳng hạn “lịch xem nhà tuần này” hoặc “lịch ngày 20/10”.",
                    "NEEDS_CLARIFICATION", request_id, context=context, scope=scope)
            return self._tool_unavailable(request_id, context, scope)

    # ------------------------------------------------------------------ orchestration

    async def _run(self, question: str, route: Route, context: dict[str, Any] | None, scope: list[str],
                   request_id: str) -> AskResult:
        folded = fold(question)
        budget = _Budget()
        result = await self._dispatch(question, folded, route, context, scope, budget, request_id)
        if route.kind == "APPOINTMENT_AND_RAG" and self.rag is not None and result.status in {"ANSWERED", "NO_RESULTS"}:
            return await self._combine(question, route, result, request_id)
        return result

    async def _dispatch(self, question: str, folded: str, route: Route, context: dict[str, Any] | None,
                        scope: list[str], budget: _Budget, request_id: str) -> AskResult:
        if route.kind == "AVAILABILITY" or (route.action == "BOOK" and scope and (
                ctx.LISTING_REF.search(folded) or ctx.LISTING_WORD_REF.search(folded)
                or ctx.LISTING_DEMONSTRATIVE.search(folded))):
            return await self._availability(question, folded, route, scope, context, budget, request_id)
        if route.action == "BOOK":
            return self._book_guidance(request_id, context, scope)
        if route.action:
            return await self._action(question, folded, route.action, context, scope, budget, request_id)
        return await self._query(question, folded, route, context, scope, budget, request_id)

    # ------------------------------------------------------------------ appointment queries

    async def _query(self, question: str, folded: str, route: Route, context: dict[str, Any] | None,
                     scope: list[str], budget: _Budget, request_id: str) -> AskResult:
        follow = route.follow_up and context is not None
        reference = resolve_reference(question, context) if follow else Reference()
        has_ref_words = bool(ctx.ORDINAL_DIGIT.search(folded) or ctx.ORDINAL_WORD.search(folded)
                             or ctx.DEMONSTRATIVE.search(folded))
        if follow and ctx.PAGING.search(folded):
            return await self._next_page(context, scope, budget, request_id)
        if follow and reference.out_of_range:
            return self._clarify_range(request_id, context, scope)
        filters_in_question = self._filters(folded, None)
        wants_detail = follow and (has_ref_words or (
            not filters_in_question[1] and not COUNT_INTENT.search(folded)
            and (len(context["appointmentIds"]) == 1 or context.get("selectedAppointmentId"))))
        if wants_detail:
            if reference.appointment_id is None:
                return await self._choose(context, scope, budget, request_id)
            return await self._detail(reference.appointment_id, folded, context, scope, budget, request_id)

        inherit = context["filters"] if follow and context else None
        inherit_role = context.get("role") if follow and context else None
        args, signals = self._filters(folded, inherit)
        if follow and inherit_role and "role" not in args and inherit_role != "BOTH":
            args["role"] = inherit_role
        if COUNT_INTENT.search(folded):
            return await self._count(args, folded, context, scope, budget, request_id)
        if NEXT_INTENT.search(folded) and not follow:
            args = {**{k: v for k, v in args.items() if k not in {"timePhase", "sort"}},
                    "timePhase": ["UPCOMING", "IN_PROGRESS"], "sort": "TIME_ASC",
                    "status": args.get("status") or ACTIVE}
            return await self._list({**args, "size": 1}, folded, context, scope, budget, request_id)
        if not signals and not follow and len(folded.split()) > 5 and self._can_plan():
            planned = await self._plan_with_model(question, context, scope, budget, request_id)
            if planned is not None:
                return planned
        return await self._list({**args, "size": PAGE_SIZE}, folded, context, scope, budget, request_id)

    def _filters(self, folded: str, inherit: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
        """Rule-based filters from the question. Second value: did the question itself carry any filter signal."""
        args: dict[str, Any] = {}
        signals = False
        if inherit:
            args.update({k: v for k, v in inherit.items() if k in {"status", "listingId", "q", "role"}})
        if OWNER_ROLE.search(folded):
            args["role"], signals = "OWNER", True
        elif RENTER_ROLE.search(folded):
            args["role"], signals = "RENTER", True
        statuses = [name for name, pattern in STATUS_PATTERNS if pattern.search(folded)]
        if "PENDING" in statuses and re.search(r"\bchua\b", folded):
            statuses = [s for s in statuses if s != "CONFIRMED"]  # 'chưa được xác nhận' means pending
        if statuses:
            args["status"], signals = statuses, True
        phases: list[str] = []
        if UPCOMING_WORDS.search(folded):
            phases = ["UPCOMING", "IN_PROGRESS"]
        elif PAST_WORDS.search(folded):
            phases = ["PAST"]
        elif LIVE_WORDS.search(folded):
            phases = ["IN_PROGRESS"]
        if phases:
            args["timePhase"], signals = phases, True
        for key in ("datePreset", "dateFrom", "dateTo"):
            args.pop(key, None)
        today = self.today()
        explicit = ctx.explicit_date(folded, today)
        weekday = next((name for name, pattern in ctx.WEEKDAY_WORDS if re.search(pattern, folded)), None)
        preset = ctx.date_preset(folded)
        if explicit:
            args["dateFrom"] = args["dateTo"] = explicit.isoformat()
            signals = True
        elif weekday:
            target = today + timedelta(days=(WEEKDAY_ENUM.index(weekday) - today.weekday()) % 7)
            if re.search(r"\btuan sau\b|\btuan toi\b", folded):
                monday = today - timedelta(days=today.weekday())
                target = monday + timedelta(days=7 + WEEKDAY_ENUM.index(weekday))
            args["dateFrom"] = args["dateTo"] = target.isoformat()
            signals = True
        elif preset:
            args["datePreset"], signals = preset, True
        window = ctx.part_of_day(folded)
        hour = ctx.clock_hour(folded)
        if hour:
            args["timeFrom"] = f"{hour[0]:02d}:{hour[1]:02d}"
            end = (datetime(2000, 1, 1, hour[0], hour[1]) + timedelta(minutes=59))
            args["timeTo"] = end.strftime("%H:%M") if end.day == 1 else "23:59"
            signals = True
        elif window:
            args["timeFrom"], args["timeTo"] = window
            signals = True
        if RESCHEDULE_WORDS.search(folded):
            args["rescheduleRequested"], signals = True, True
        # Questions about 'what is on' a day/phase mean the live and finished ones, not cancelled/rejected/expired.
        if "status" not in args and (("datePreset" in args or "dateFrom" in args or "timePhase" in args)
                                     and "PAST" not in args.get("timePhase", [])):
            args["status"] = list(ACTIVE if args.get("timePhase") else LIVE_AND_DONE)
        elif "status" not in args and args.get("timePhase") == ["PAST"]:
            args["status"] = list(LIVE_AND_DONE)
        return args, signals

    async def _call(self, budget: _Budget, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        budget.spend()
        return await self.tools.call(name, arguments)

    async def _list(self, args: dict[str, Any], folded: str, context: dict[str, Any] | None, scope: list[str],
                    budget: _Budget, request_id: str) -> AskResult:
        found = await self._call(budget, "search_my_appointments", args)
        return self._list_result(found, request_id, context, scope)

    async def _next_page(self, context: dict[str, Any], scope: list[str], budget: _Budget, request_id: str) -> AskResult:
        page = context.get("page")
        if not page or not page.get("hasNext"):
            return self._result("Danh sách vừa rồi đã hết, không còn lịch nào khác khớp điều kiện đó. "
                                "Bạn có thể đổi điều kiện, ví dụ “lịch tuần sau”.", "ANSWERED", request_id,
                                context=context, scope=scope, cards=[])
        args = {**context["filters"], "page": page["page"] + 1, "size": page["size"]}
        if context.get("role") and context["role"] != "BOTH":
            args["role"] = context["role"]
        found = await self._call(budget, "search_my_appointments", args)
        return self._list_result(found, request_id, context, scope)

    async def _choose(self, context: dict[str, Any], scope: list[str], budget: _Budget, request_id: str) -> AskResult:
        """The reference is ambiguous: re-read the same list and ask which one."""
        args = {**context["filters"], "size": PAGE_SIZE}
        if context.get("role") and context["role"] != "BOTH":
            args["role"] = context["role"]
        found = await self._call(budget, "search_my_appointments", args)
        return self._list_result(found, request_id, context, scope, ask_which=True)

    async def _count(self, args: dict[str, Any], folded: str, context: dict[str, Any] | None, scope: list[str],
                     budget: _Budget, request_id: str) -> AskResult:
        filters = {k: v for k, v in args.items() if k not in {"sort", "page", "size"}}
        summary = await self._call(budget, "summarize_my_appointments", filters)
        total = int(summary.get("total") or 0)
        cards: list[dict[str, Any]] = []
        new_context = make_context([], None, summary.get("filters", {}).get("role"), filters, None, None)
        if 0 < total <= MAX_CARDS:
            found = await self._call(budget, "search_my_appointments", {**filters, "size": MAX_CARDS})
            cards = [c for c in (self._card(i, found.get("meta")) for i in found.get("items") or []) if c]
            new_context = self._list_context(found, cards)
        elif summary.get("nextUpcoming"):
            card = self._card(summary["nextUpcoming"], summary.get("meta"))
            cards = [card] if card else []
            new_context = make_context([card["appointmentId"]] if card else [], None,
                                       (summary.get("filters") or {}).get("role"), filters, None,
                                       (summary.get("meta") or {}).get("today"))
        return self._result(self._summary_text(summary, folded), "ANSWERED" if total else "NO_RESULTS", request_id,
                            context=new_context, scope=scope, cards=cards)

    async def _detail(self, appointment_id: str, folded: str, context: dict[str, Any] | None, scope: list[str],
                      budget: _Budget, request_id: str) -> AskResult:
        detail = await self._call(budget, "get_appointment_detail", {"appointmentId": appointment_id})
        card = self._card(detail, detail.get("meta"))
        new_context = make_context([appointment_id], appointment_id, detail.get("requesterRole"),
                                   (context or {}).get("filters"), None, (detail.get("meta") or {}).get("today"))
        return self._result(self._detail_text(detail, folded), "ANSWERED", request_id, context=new_context,
                            scope=scope, cards=[card] if card else [])

    # ------------------------------------------------------------------ model fallback (bounded)

    def _can_plan(self) -> bool:
        return self.generative_client is not None and self.settings.generation_provider == "openai"

    async def _plan_with_model(self, question: str, context: dict[str, Any] | None, scope: list[str],
                               budget: _Budget, request_id: str) -> AskResult | None:
        """For questions the rules cannot place. Bounded rounds and calls; the planner sees ids and tool names only."""
        known = list((context or {}).get("appointmentIds") or [])
        done: list[dict[str, Any]] = []
        results: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for _ in range(MAX_MODEL_ROUNDS):
            try:
                plans = await self.planner.plan_round(question, known, scope, done, budget.left)
            except GenerationUnavailableError as error:
                logger.warning("appointment_planner_failed", request_id=request_id, error_type=type(error).__name__)
                break
            fresh = [p for p in plans if (p.name, repr(sorted(p.arguments.items()))) not in
                     {(n, repr(sorted(a.items()))) for n, a, _ in results}]
            if not fresh or budget.left <= 0:
                break
            for plan in fresh[:budget.left]:
                data = await self._call(budget, plan.name, plan.arguments)
                results.append((plan.name, plan.arguments, data))
                done.append({"tool": plan.name})  # names only: untrusted text never reaches the planner
                if plan.name == "search_my_appointments":
                    known.extend(i.get("appointmentId") for i in data.get("items") or [])
            if len(results) >= 3:
                break
        if not results:
            return None
        return self._compose_planned(results, request_id, context, scope)

    def _compose_planned(self, results: list[tuple[str, dict[str, Any], dict[str, Any]]], request_id: str,
                         context: dict[str, Any] | None, scope: list[str]) -> AskResult:
        parts: list[str] = []
        cards: list[dict[str, Any]] = []
        new_context = context
        status = "ANSWERED"
        for name, arguments, data in results:
            if name == "search_my_appointments":
                one = self._list_result(data, request_id, context, scope)
                parts.append(one.answer)
                cards.extend(one.appointment_cards)
                new_context = one.appointment_context
                status = one.status if len(results) == 1 else status
            elif name == "summarize_my_appointments":
                parts.append(self._summary_text(data, ""))
            elif name == "get_appointment_detail":
                card = self._card(data, data.get("meta"))
                cards.extend([card] if card else [])
                parts.append(self._detail_text(data, ""))
                new_context = make_context([data.get("appointmentId")], data.get("appointmentId"),
                                           data.get("requesterRole"), None, None, (data.get("meta") or {}).get("today"))
            elif name == "get_listing_viewing_availability":
                parts.append(self._availability_text(data, None, None))
        deduped = list({c["appointmentId"]: c for c in cards}.values())[:MAX_CARDS]
        return self._result("\n\n".join(parts), status, request_id, context=new_context, scope=scope, cards=deduped)

    # ------------------------------------------------------------------ actions: explain, never perform

    async def _action(self, question: str, folded: str, action: str, context: dict[str, Any] | None,
                      scope: list[str], budget: _Budget, request_id: str) -> AskResult:
        reference = resolve_reference(question, context)
        if reference.out_of_range:
            return self._clarify_range(request_id, context, scope)
        appointment_id = reference.appointment_id
        if appointment_id is None and context and reference.ambiguous:
            result = await self._choose(context, scope, budget, request_id)
            return self._prefix(result, self._never_acts(action))
        if appointment_id is None:
            found = await self._call(budget, "search_my_appointments",
                                     {"status": list(ACTIVE), "sort": "TIME_ASC", "size": PAGE_SIZE})
            items = found.get("items") or []
            if not items:
                return self._result(
                    f"{self._never_acts(action)} Hiện bạn cũng chưa có lịch hẹn nào đang chờ xác nhận hoặc đã xác nhận "
                    "để thao tác.", "NO_RESULTS", request_id, context=make_context([]), scope=scope)
            if len(items) > 1 or int(found.get("page", {}).get("totalElements") or len(items)) > 1:
                return self._prefix(self._list_result(found, request_id, context, scope, ask_which=True),
                                    self._never_acts(action))
            appointment_id = items[0].get("appointmentId")
        detail = await self._call(budget, "get_appointment_detail", {"appointmentId": appointment_id})
        card = self._card(detail, detail.get("meta"))
        new_context = make_context([appointment_id], appointment_id, detail.get("requesterRole"), None, None,
                                   (detail.get("meta") or {}).get("today"))
        return self._result(self._action_text(action, detail), "ANSWERED", request_id, context=new_context,
                            scope=scope, cards=[card] if card else [])

    @staticmethod
    def _never_acts(action: str) -> str:
        return (f"Mình chỉ xem và giải thích lịch hẹn, không thể tự {ACTION_LABELS.get(action, 'thay đổi')} "
                "thay bạn hay gửi thông báo cho ai.")

    @staticmethod
    def _location(owner: bool) -> str:
        return ("mục “Lịch xem nhà” trong nhóm Quản lý của trang quản lý" if owner
                else "mục “Lịch đi xem” trong nhóm Thuê nhà của trang quản lý")

    def _action_text(self, action: str, detail: dict[str, Any]) -> str:
        owner = detail.get("requesterRole") == "OWNER"
        lines = [self._never_acts(action)]
        role_actions = OWNER_ACTIONS if owner else RENTER_ACTIONS
        candidates = {"APPROVE": ["APPROVE", "APPROVE_RESCHEDULE"], "REJECT": ["REJECT", "REJECT_RESCHEDULE"]}.get(
            action, [action])
        if detail.get("rescheduleRequested") is False:
            candidates = [c for c in candidates if not c.endswith("_RESCHEDULE")] or candidates
        elif detail.get("rescheduleRequested") and action in {"APPROVE", "REJECT"}:
            candidates = [c for c in candidates if c.endswith("_RESCHEDULE")] + [c for c in candidates if not c.endswith("_RESCHEDULE")]
        if not any(c in role_actions for c in candidates):
            if action == "REQUEST_RESCHEDULE":
                lines.append("Chỉ người đi xem mới đề xuất đổi giờ. Với lịch này bạn là chủ nhà nên bạn có thể chấp nhận "
                             "hoặc từ chối đề xuất của khách khi có.")
            elif action in {"APPROVE", "REJECT", "COMPLETE", "APPROVE_RESCHEDULE", "REJECT_RESCHEDULE"}:
                lines.append(f"Việc {ACTION_LABELS.get(action)} do chủ nhà thực hiện. Với lịch này bạn là người đi xem, "
                             "nên bạn có thể hủy lịch hoặc đề xuất đổi giờ.")
            return "\n\n".join(lines) + "\n\n" + self._detail_text(detail, "", short=True)
        actions = detail.get("actions") or {}
        available = [c for c in candidates if c in (actions.get("available") or [])]
        blocked = actions.get("blocked") or {}
        if available:
            label = ACTION_LABELS[available[0]]
            lines.append(f"Theo trạng thái vừa đọc, bạn có thể {label} lịch này tại {self._location(owner)}: mở đúng lịch "
                         "và dùng thao tác tương ứng. Hệ thống sẽ kiểm tra lại điều kiện (ví dụ thời gian mới bạn đề xuất) "
                         "khi bạn xác nhận.")
        else:
            reasons = []
            for candidate in candidates:
                reasons.extend(BLOCK_TEXT.get(code, "điều kiện hiện tại chưa cho phép") for code in blocked.get(candidate, []))
            reason = "; ".join(dict.fromkeys(reasons)) or "điều kiện hiện tại chưa cho phép"
            lines.append(f"Hiện chưa {ACTION_LABELS.get(candidates[0], 'thao tác')} được vì {reason}.")
        return "\n\n".join(lines) + "\n\n" + self._detail_text(detail, "", short=True)

    def _book_guidance(self, request_id: str, context: dict[str, Any] | None, scope: list[str]) -> AskResult:
        return self._result(
            self._never_acts("BOOK") + " Để đặt lịch, bạn mở trang tin đăng bạn quan tâm rồi bấm đặt lịch xem nhà và chọn "
            "ngày giờ còn trống. Hệ thống sẽ kiểm tra khung giờ và xác minh danh tính (KYC) khi bạn gửi yêu cầu, và chủ "
            "nhà cần xác nhận. Nếu muốn, bạn cho mình biết tin nào (ví dụ “căn thứ hai”) và ngày nào, mình sẽ xem khung "
            "giờ còn trống giúp bạn.", "ANSWERED", request_id, context=context, scope=scope)

    # ------------------------------------------------------------------ availability

    async def _availability(self, question: str, folded: str, route: Route, scope: list[str],
                            context: dict[str, Any] | None, budget: _Budget, request_id: str) -> AskResult:
        listing_id, out_of_range, ambiguous = resolve_listing(question, scope)
        if out_of_range:
            return self._result(f"Danh sách tin vừa rồi chỉ có {len(scope)} tin. Bạn cho mình đúng số thứ tự trong danh sách "
                                "nhé.", "NEEDS_CLARIFICATION", request_id, context=context, scope=scope)
        if ambiguous or listing_id is None:
            return self._result(
                f"Bạn đang có {len(scope)} tin trong danh sách vừa rồi. Bạn muốn xem khung giờ của tin nào "
                "(ví dụ “căn thứ hai”) và vào ngày nào?", "NEEDS_CLARIFICATION", request_id, context=context, scope=scope)
        plans = self._availability_plans(ctx.strip_listing_ordinals(folded))
        if plans is None:
            return self._result("Bạn muốn xem nhà vào ngày cụ thể nào (ví dụ thứ Bảy, ngày mai hoặc 20/10)? "
                                "Mình sẽ xem khung giờ còn trống của ngày đó.", "NEEDS_CLARIFICATION", request_id,
                                context=context, scope=scope)
        results = []
        for arguments in plans:
            results.append(await self._call(budget, "get_listing_viewing_availability",
                                            {"listingId": listing_id, **arguments}))
        folded = ctx.strip_listing_ordinals(folded)
        window = ctx.part_of_day(folded)
        hour = ctx.clock_hour(folded)
        text = "\n\n".join(self._availability_text(r, window, hour, defaulted=not any(
            [ctx.explicit_date(folded, self.today()), ctx.date_preset(folded)] + [
                bool(re.search(p, folded)) for _, p in ctx.WEEKDAY_WORDS])) for r in results)
        if route.action == "BOOK":
            text += "\n\n" + self._never_acts("BOOK") + " Bạn bấm đặt lịch xem nhà ngay trên trang tin đăng để gửi yêu cầu."
        result = self._result(text, "ANSWERED", request_id, context=None, scope=scope, cards=[])
        result.listing_ids = [listing_id]
        return result

    def _availability_plans(self, folded: str) -> list[dict[str, Any]] | None:
        explicit = ctx.explicit_date(folded, self.today())
        if explicit:
            return [{"date": explicit.isoformat()}]
        weekday = next((name for name, pattern in ctx.WEEKDAY_WORDS if re.search(pattern, folded)), None)
        week = "NEXT" if re.search(r"\btuan sau\b|\btuan toi\b", folded) else "THIS" if re.search(r"\btuan nay\b", folded) else None
        if weekday:
            return [{"weekday": weekday, **({"week": week} if week else {})}]
        if re.search(r"\bcuoi tuan\b", folded):
            return [{"weekday": "SATURDAY", **({"week": week} if week else {})},
                    {"weekday": "SUNDAY", **({"week": week} if week else {})}]
        if re.search(r"\bngay kia\b", folded):
            return [{"datePreset": "DAY_AFTER_TOMORROW"}]
        preset = ctx.date_preset(folded)
        if preset in {"TODAY", "TOMORROW"}:
            return [{"datePreset": preset}]
        if preset or week:
            return None  # 'tuần sau', 'tháng này': a range, not a day - ask which day
        return [{"datePreset": "TODAY"}]

    def _availability_text(self, data: dict[str, Any], window: tuple[str, str] | None,
                           hour: tuple[int, int] | None, defaulted: bool = False) -> str:
        meta = data.get("meta") or {}
        listing = _listing_text(data.get("listing"))
        day = f"{_weekday(data.get('date'))}, {_fmt_date(data.get('date'))}"
        lines = [f"Theo dữ liệu hệ thống đọc lúc {_fmt_instant(meta.get('readAt'))}, tình trạng khung giờ xem nhà "
                 f"của **{listing}** vào **{day}**" + (" (bạn không nêu ngày nên mình xem hôm nay)" if defaulted else "") + ":"]
        if data.get("callerIsOwner"):
            lines.append("- Đây là tin của bạn nên bạn không đặt lịch xem chính tin của mình được; bạn có thể theo dõi "
                         "yêu cầu của khách trong mục Lịch xem nhà.")
        if not data.get("dayAccepted"):
            allowed = ", ".join(WEEKDAYS_VI[WEEKDAY_ENUM.index(d)] for d in data.get("allowedViewingDays") or []
                                if d in WEEKDAY_ENUM)
            lines.append("- Ngày này chủ nhà không nhận lịch xem" + (f" (chủ nhà nhận: {allowed})" if allowed else "") + ".")
            return "\n".join(lines)
        if data.get("callerActiveBookingId"):
            lines.append("- Bạn đang có một yêu cầu xem nhà còn hiệu lực với tin này; mỗi tin chỉ được một yêu cầu đang "
                         "chờ hoặc đã xác nhận cùng lúc.")
        slots = list(data.get("slots") or [])
        if window:
            slots = [s for s in slots if window[0] <= _hm(s.get("startTime")) <= window[1]]
        if hour:
            wanted = f"{hour[0]:02d}:{hour[1]:02d}"
            slots = [s for s in slots if _hm(s.get("startTime")) <= wanted < _hm(s.get("endTime"))]
        if (window or hour) and not slots:
            lines.append("- Không có khung giờ nào của ngày này nằm trong thời gian bạn hỏi.")
            return "\n".join(lines)
        if not slots:
            lines.append("- Hệ thống không có khung giờ nào để đặt trong ngày này (ví dụ ngày đã qua).")
            return "\n".join(lines)
        grouped: dict[str, list[str]] = {}
        for slot in slots:
            grouped.setdefault(slot.get("status") or "NOT_AVAILABLE", []).append(
                f"{_hm(slot.get('startTime'))}–{_hm(slot.get('endTime'))}")
        titles = {"CAN_REQUEST": "Có thể gửi yêu cầu", "YOUR_REQUEST_PENDING": "Yêu cầu của bạn đang chờ xác nhận",
                  "YOUR_BOOKING_CONFIRMED": "Lịch của bạn đã được xác nhận",
                  "OWNER_CANNOT_BOOK_OWN_LISTING": "Khung giờ trong tin của bạn", "NOT_AVAILABLE": "Không đặt được"}
        for state in SLOT_STATE_ORDER:
            if state in grouped:
                lines.append(f"- **{titles[state]}:** " + ", ".join(grouped[state]))
        if grouped.get("CAN_REQUEST"):
            lines.append("")
            lines.append("Đây là khung giờ có thể gửi yêu cầu, chưa phải lịch đã được giữ chỗ: chủ nhà vẫn cần xác nhận, "
                         "và hệ thống kiểm tra thêm (như xác minh danh tính) khi bạn gửi yêu cầu.")
        elif not grouped.get("YOUR_REQUEST_PENDING") and not grouped.get("YOUR_BOOKING_CONFIRMED"):
            lines.append("")
            lines.append("Hiện không còn khung giờ nào bạn gửi yêu cầu được trong phạm vi này. Bạn có thể hỏi ngày khác.")
        if any(s.get("bookingCount") for s in slots if s.get("status") == "CAN_REQUEST"):
            lines.append("Một khung giờ có thể đã có yêu cầu từ người khác, nhưng điều đó không có nghĩa là khung giờ đã kín.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ answers

    def _lead_time(self, meta: dict[str, Any] | None) -> str:
        return _fmt_instant((meta or {}).get("readAt"))

    def _describe(self, filters: dict[str, Any] | None) -> tuple[str, str]:
        """(subject, qualifiers) from the filters Core resolved, so the answer states exactly what was asked."""
        f = filters or {}
        role = f.get("role")
        qualifiers: list[str] = []
        rng = f.get("dateRange")
        if rng and rng.get("from"):
            label = {"TODAY": "hôm nay", "TOMORROW": "ngày mai", "THIS_WEEK": "tuần này", "NEXT_WEEK": "tuần sau",
                     "THIS_WEEKEND": "cuối tuần này", "THIS_MONTH": "tháng này"}.get(rng.get("preset") or "")
            if rng["from"] == rng["to"]:
                qualifiers.append(f"vào {(label + ', ') if label else ''}{_weekday(rng['from'])} {_fmt_date(rng['from'])}")
            else:
                qualifiers.append(f"{label + ' ' if label else ''}từ {_fmt_date(rng['from'])} đến {_fmt_date(rng['to'])}")
        if f.get("timeFrom") or f.get("timeTo"):
            qualifiers.append(f"trong khoảng {_hm(f.get('timeFrom') or '00:00')}–{_hm(f.get('timeTo') or '23:59')}")
        phases = f.get("timePhases") or []
        if phases:
            qualifiers.append("(" + ", ".join(PHASE_LABELS[p].lower() for p in phases if p in PHASE_LABELS) + ")")
        statuses = f.get("statuses") or []
        if statuses and len(statuses) < len(STATUS_LABELS):
            qualifiers.append("ở trạng thái " + ", ".join(STATUS_LABELS[s].lower() for s in statuses if s in STATUS_LABELS))
        if f.get("rescheduleRequested"):
            qualifiers.append("có đề xuất đổi giờ đang chờ")
        subject = {"OWNER": "khách đã đặt xem nhà của bạn", "RENTER": "bạn đăng ký đi xem"}.get(role, "")
        return subject, " ".join(qualifiers)

    def _list_result(self, found: dict[str, Any], request_id: str, context: dict[str, Any] | None,
                     scope: list[str], ask_which: bool = False) -> AskResult:
        meta = found.get("meta") or {}
        items = found.get("items") or []
        page = found.get("page") or {}
        filters = found.get("filters") or {}
        subject, qualifier = self._describe(filters)
        total = int(page.get("totalElements") if page.get("totalElements") is not None else len(items))
        when = f" {qualifier}" if qualifier else ""
        if not items:
            who = {"OWNER": "khách nào đặt xem nhà của bạn", "RENTER": "lịch bạn đăng ký đi xem"}.get(filters.get("role"), "lịch xem nhà nào")
            text = (f"Theo dữ liệu hệ thống đọc lúc {self._lead_time(meta)}, hiện không có {who}{when}.")
            return self._result(text, "NO_RESULTS", request_id, context=make_context([]), scope=scope)
        cards = [c for c in (self._card(i, meta) for i in items[:MAX_CARDS]) if c]
        size = int(page.get("size") or len(items))
        offset = (int(page.get("page") or 1) - 1) * size
        head = f"Theo dữ liệu hệ thống đọc lúc {self._lead_time(meta)}, "
        if filters.get("role") == "OWNER":
            head += f"có **{total}** lịch khách đặt xem nhà của bạn{when}:"
        elif filters.get("role") == "RENTER":
            head += f"bạn có **{total}** lịch đi xem nhà{when}:"
        else:
            head += f"bạn có **{total}** lịch xem nhà{when}:"
        lines = [head, ""]
        mixed = filters.get("role") in (None, "BOTH")
        for index, item in enumerate(items[:MAX_CARDS], 1):
            parts = [_sched(item.get("currentSchedule")), _listing_text(item.get("listing"))]
            if mixed:
                parts.append("bạn là chủ nhà" if item.get("requesterRole") == "OWNER" else "bạn là người đi xem")
            parts.append(_state_text(item))
            proposed = _proposed_text(item)
            if proposed:
                parts.append(proposed)
            lines.append(f"- **Lịch {offset + index}:** " + " · ".join(parts))
        shown = offset + min(len(items), MAX_CARDS)
        if total > shown:
            lines.append(f"\nCòn {total - shown} lịch nữa; bạn nói “xem tiếp” để mình mở tiếp.")
        elif len(items) > 1:
            lines.append("\nBạn có thể nói “lịch thứ 2” để xem chi tiết một lịch.")
        if ask_which:
            lines.insert(0, "Bạn đang hỏi lịch nào trong danh sách này (ví dụ “lịch thứ 2”)?\n")
        new_context = self._list_context(found, cards)
        return self._result("\n".join(lines), "NEEDS_CLARIFICATION" if ask_which else "ANSWERED", request_id,
                            context=new_context, scope=scope, cards=cards)

    @staticmethod
    def _filters_from_resolved(filters: dict[str, Any]) -> dict[str, Any]:
        """Core's resolved filters back to request arguments (so 'xem tiếp' repeats the same query)."""
        out: dict[str, Any] = {}
        statuses = filters.get("statuses") or []
        if statuses and len(statuses) < len(STATUS_LABELS):
            out["status"] = statuses
        rng = filters.get("dateRange")
        if rng and rng.get("from"):
            if rng.get("preset"):
                out["datePreset"] = rng["preset"]
            else:
                out["dateFrom"], out["dateTo"] = rng["from"], rng["to"]
        for key, name in (("timeFrom", "timeFrom"), ("timeTo", "timeTo"), ("listingId", "listingId"),
                          ("rescheduleRequested", "rescheduleRequested"), ("q", "q"), ("sort", "sort")):
            if filters.get(key) is not None:
                out[name] = filters[key]
        if filters.get("timePhases"):
            out["timePhase"] = filters["timePhases"]
        return out

    def _list_context(self, found: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any]:
        meta = found.get("meta") or {}
        page = found.get("page") or {}
        resolved = found.get("filters") or {}
        role = resolved.get("role")
        filters = self._filters_from_resolved(resolved)
        return make_context([c["appointmentId"] for c in cards], None, role, filters,
                            {"page": page.get("page") or 1, "size": page.get("size") or PAGE_SIZE,
                             "totalPages": page.get("totalPages") or 1, "total": page.get("totalElements") or 0,
                             "hasNext": bool(page.get("hasNext"))}, meta.get("today"))

    def _summary_text(self, summary: dict[str, Any], folded: str) -> str:
        meta = summary.get("meta") or {}
        total = int(summary.get("total") or 0)
        subject, qualifier = self._describe(summary.get("filters"))
        scope = {"OWNER": "lịch khách đặt xem nhà của bạn", "RENTER": "lịch bạn đi xem nhà"}.get(
            (summary.get("filters") or {}).get("role"), "lịch xem nhà của bạn")
        lines = [f"Theo dữ liệu hệ thống đọc lúc {self._lead_time(meta)}, bạn có **{total}** {scope}"
                 f"{(' ' + qualifier) if qualifier else ''}."]
        if not total:
            return lines[0]
        by_role = summary.get("byRole") or {}
        if (summary.get("filters") or {}).get("role") in (None, "BOTH") and (by_role.get("OWNER") or by_role.get("RENTER")):
            lines.append(f"- Là chủ nhà: {by_role.get('OWNER', 0)} lịch; là người đi xem: {by_role.get('RENTER', 0)} lịch.")
        status_parts = [f"{STATUS_LABELS[s].lower()}: {n}" for s, n in (summary.get("byStatus") or {}).items() if n and s in STATUS_LABELS]
        if status_parts:
            lines.append("- Theo trạng thái: " + "; ".join(status_parts) + ".")
        phase = summary.get("byTemporalPhase") or {}
        phase_parts = [f"{label.lower()}: {phase[key]}" for key, label in PHASE_LABELS.items() if phase.get(key)]
        if phase_parts:
            lines.append("- Theo thời gian: " + "; ".join(phase_parts) + ".")
        if summary.get("pendingRescheduleRequests"):
            lines.append(f"- Đề xuất đổi giờ đang chờ xử lý: {summary['pendingRescheduleRequests']}.")
        nxt = summary.get("nextUpcoming")
        if nxt:
            lines.append(f"- Lịch sắp tới gần nhất: {_sched(nxt.get('currentSchedule'))} · {_listing_text(nxt.get('listing'))}.")
        if TOPIC_PEOPLE.search(folded):
            counted = summary.get("appointmentsCountedForVisitors")
            lines.append(f"- Tổng số người đi xem khai báo trong các yêu cầu còn hiệu lực: {summary.get('expectedVisitorsTotal', 0)}"
                         f" (tính trên {counted} lịch). Đây là số người khai báo, không phải số lịch.")
        lines.append("\nLịch đã qua giờ hẹn nhưng chưa được hệ thống cập nhật trạng thái vẫn được tính theo trạng thái đang lưu, "
                     "và không được tính là sắp tới.")
        return "\n".join(lines)

    def _detail_text(self, detail: dict[str, Any], folded: str, short: bool = False) -> str:
        meta = detail.get("meta") or {}
        owner = detail.get("requesterRole") == "OWNER"
        lines = [] if short else [f"Theo dữ liệu hệ thống đọc lúc {self._lead_time(meta)}, về lịch xem nhà này:", ""]
        lines.append(f"- **Tin đăng:** {_listing_text(detail.get('listing'))}")
        lines.append(f"- **Thời gian hiện tại:** {_sched(detail.get('currentSchedule'))}")
        lines.append(f"- **Vai trò của bạn:** {'chủ nhà' if owner else 'người đi xem'}")
        lines.append(f"- **Tình trạng:** {_state_text(detail)}")
        proposed = _proposed_text(detail)
        if proposed:
            lines.append(f"- **Đổi giờ:** {proposed}")
            if detail.get("rescheduleReason") and not short:
                lines.append(f"  Lý do người đi xem nêu: “{_clean(detail['rescheduleReason'])}”")
        if detail.get("visitorCount") is not None and (not short and (owner or TOPIC_PEOPLE.search(folded))):
            lines.append(f"- **Số người đi xem khai báo:** {detail['visitorCount']}")
        if owner and detail.get("renterName") and (TOPIC_WHO.search(folded) or not folded):
            lines.append(f"- **Người đặt:** {_clean(detail['renterName'], 60)}")
        if detail.get("status") == "REJECTED" and detail.get("rejectReason"):
            lines.append(f"- **Lý do từ chối:** “{_clean(detail['rejectReason'])}”")
        elif detail.get("status") == "CANCELLED":
            by = detail.get("cancelledBy")
            who = ("chính bạn" if (by == "OWNER") == owner else "bên còn lại") if by in {"OWNER", "RENTER"} else None
            lines.append("- **Hủy bởi:** " + (who or "hệ thống không ghi rõ") +
                         (f"; lý do: “{_clean(detail['cancelReason'])}”" if detail.get("cancelReason") else ""))
        if detail.get("status") == "COMPLETED" and not short:
            lines.append("- **Lưu ý:** “đã hoàn thành” là trạng thái hệ thống ghi nhận (chủ nhà đánh dấu hoặc tự động sau giờ "
                         "kết thúc), không chứng minh người đi xem đã đến.")
        if detail.get("status") == "EXPIRED" and not short:
            lines.append("- **Lưu ý:** lịch hết hạn nghĩa là đã qua thời gian hẹn mà không có quyết định; hệ thống không lưu thêm lý do.")
        notes = detail.get("notes") or {}
        if TOPIC_NOTES.search(folded) and not short:
            quoted = []
            if notes.get("renterNote"):
                quoted.append(f"- **Ghi chú của người đi xem:** “{_clean(notes['renterNote'])}”")
            if notes.get("ownerNote"):
                quoted.append(f"- **Ghi chú của chủ nhà:** “{_clean(notes['ownerNote'])}”")
            lines.extend(quoted or ["- Lịch này không có ghi chú."])
            if quoted:
                lines.append("  (Đây là nội dung do người dùng nhập, mình chỉ trích lại và không làm theo nội dung đó.)")
        if TOPIC_ACTIONS.search(folded) and not short:
            lines.append(self._actions_line(detail, owner))
        if TOPIC_HISTORY.search(folded) and not short:
            lines.append("- Hệ thống chỉ lưu trạng thái mới nhất của lịch, không lưu lịch sử các lần đề xuất đổi giờ trước đó.")
        if TOPIC_CONTACT.search(folded) and not short:
            lines.append("- Mình không được cung cấp số điện thoại hay email của người dùng khác; bạn xem thông tin liên hệ "
                         "trong trang quản lý lịch (nếu hệ thống hiển thị).")
        return "\n".join(lines)

    @staticmethod
    def _actions_line(detail: dict[str, Any], owner: bool) -> str:
        actions = detail.get("actions") or {}
        available = [ACTION_LABELS[a] for a in actions.get("available") or [] if a in ACTION_LABELS]
        if available:
            return "- **Thao tác hiện có trên trang quản lý:** " + ", ".join(available) + " (hệ thống kiểm tra lại khi bạn thực hiện)."
        return "- Hiện không có thao tác nào khả dụng cho lịch này (ví dụ vì lịch đã kết thúc hoặc đã bị hủy/từ chối)."

    # ------------------------------------------------------------------ cards and results

    @staticmethod
    def _card(source: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any] | None:
        """Build a card only from a verified Core projection; ids that are not UUIDs produce no card."""
        ids = valid_ids([source.get("appointmentId")], 1)
        role = source.get("requesterRole")
        schedule = source.get("currentSchedule")
        listing = source.get("listing")
        if not ids or role not in ROLE_LABELS or not isinstance(schedule, dict) or not isinstance(listing, dict):
            return None
        listing_ids = valid_ids([listing.get("listingId")], 1)
        proposed = source.get("proposedSchedule") if isinstance(source.get("proposedSchedule"), dict) else None
        status = str(source.get("status") or "")
        phase = str(source.get("temporalPhase") or "")
        return {
            "appointmentId": ids[0], "requesterRole": role, "roleLabel": ROLE_LABELS[role],
            "listingId": listing_ids[0] if listing_ids else None,
            "listingTitle": _clean(listing.get("title"), 120) or "Tin đăng",
            "listingAddress": _clean(listing.get("address"), 160) or None,
            "date": schedule.get("date"), "startTime": _hm(schedule.get("startTime")),
            "endTime": _hm(schedule.get("endTime")),
            "status": status, "statusLabel": STATUS_LABELS.get(status, status),
            "temporalPhase": phase, "phaseLabel": PHASE_LABELS.get(phase, phase),
            "statusSyncPending": bool(source.get("statusSyncPending")),
            "rescheduleRequested": bool(source.get("rescheduleRequested")),
            "proposedDate": proposed.get("date") if proposed else None,
            "proposedStartTime": _hm(proposed.get("startTime")) if proposed else None,
            "proposedEndTime": _hm(proposed.get("endTime")) if proposed else None,
            "readAt": (meta or {}).get("readAt"), "source": "CORE",
        }

    def _result(self, text: str, status: str, request_id: str, *, context: dict[str, Any] | None = None,
                scope: list[str] | None = None, cards: list[dict[str, Any]] | None = None) -> AskResult:
        from homespace_ai.application.listing_context import context_for
        return AskResult(
            answer=text, status=status, citations=[], request_id=request_id,
            # The listing scope is preserved (references only) so "căn thứ hai" still works after this answer.
            listing_context=context_for(scope) if scope else None,
            appointment_context=context, appointment_cards=cards or [],
            # Every appointment result names its module, errors included, so the UI never words an appointment
            # failure as a contract or listing failure. The UI decides from `status` whether a source line is shown.
            data_sources=[SOURCE])

    @staticmethod
    def _prefix(result: AskResult, text: str) -> AskResult:
        result.answer = f"{text}\n\n{result.answer}"
        return result

    def _clarify_range(self, request_id: str, context: dict[str, Any] | None, scope: list[str]) -> AskResult:
        count = len(context["appointmentIds"]) if context else 0
        return self._result(
            f"Danh sách vừa rồi chỉ có {count} lịch. Bạn cho mình số thứ tự đúng trong danh sách nhé; mình sẽ đọc lại dữ "
            "liệu mới nhất.", "NEEDS_CLARIFICATION", request_id, context=context, scope=scope)

    @staticmethod
    def _login_required(request_id: str) -> AskResult:
        return AskResult(
            answer="Để xem lịch hẹn xem nhà của bạn, bạn cần đăng nhập HomeSpace: mình chỉ đọc được lịch thuộc tài khoản "
                   "đang đăng nhập. Nếu muốn biết quy trình chung (ví dụ cách đặt lịch hay đổi lịch), bạn cứ hỏi không kèm "
                   "thông tin cá nhân nhé.", status="LOGIN_REQUIRED", citations=[], request_id=request_id,
            data_sources=[SOURCE])

    def _tool_unavailable(self, request_id: str, context: dict[str, Any] | None, scope: list[str]) -> AskResult:
        result = self._result(
            "Mình chưa đọc được dữ liệu lịch hẹn của bạn lúc này, nên không muốn đoán là bạn có hay không có lịch. "
            "Bạn thử lại sau ít phút hoặc xem trực tiếp trong trang quản lý lịch xem nhà nhé.",
            "TOOL_UNAVAILABLE", request_id, context=context, scope=scope)
        return result

    # ------------------------------------------------------------------ RAG combination

    RAG_QUERIES: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"\bdoi\b"), "Đổi lịch xem nhà trên HomeSpace được thực hiện như thế nào và ai là người quyết định?"),
        (re.compile(r"\bhuy\b"), "Cách hủy lịch hẹn xem nhà trên HomeSpace như thế nào?"),
        (re.compile(r"\bduyet\b|\bchap nhan\b|\btu choi\b|\bxac nhan\b"), "Chủ nhà xác nhận hoặc từ chối yêu cầu xem nhà trên HomeSpace như thế nào?"),
        (re.compile(r"\bdat lich\b|\bhen xem\b"), "Quy trình đặt lịch xem nhà trên HomeSpace gồm những bước nào?"),
    ]

    async def _combine(self, question: str, route: Route, result: AskResult, request_id: str) -> AskResult:
        """Personal facts and general guidance stay in separate, labelled sections with separate sources."""
        folded = fold(question)
        general_question = next((text for pattern, text in self.RAG_QUERIES if pattern.search(folded)),
                                "Quy trình đặt lịch và quản lý lịch xem nhà trên HomeSpace là gì?")
        try:
            guidance = await self.rag(general_question)  # type: ignore[misc]
        except Exception as error:  # a failed guide must not erase the personal answer
            logger.warning("appointment_rag_failed", request_id=request_id, error_type=type(error).__name__)
            guidance = None
        usable = guidance is not None and guidance.status == "ANSWERED" and bool(guidance.citations)
        general = (guidance.answer.strip() if usable else
                   "Hiện tài liệu HomeSpace chưa có hướng dẫn chung phù hợp cho nội dung này.")
        result.answer = (f"**Thông tin lịch hẹn của bạn**\n\n{result.answer}\n\n"
                         f"**Hướng dẫn chung từ tài liệu HomeSpace** (không phải dữ liệu riêng của bạn)\n\n{general}")
        result.citations = guidance.citations if usable else []
        result.data_sources = [SOURCE, "RAG"] if usable else [SOURCE]
        return result
