"""Read-only viewing-appointment tools for the signed-in account.

Safety properties enforced here (not left to the model):
- a fixed table of tools -> fixed Core paths, GET only; the model cannot choose a path, method, host or header;
- strict argument whitelists (enum / range / UUID / date / time); unknown keys are rejected;
- the caller identity is never an argument: Core derives it from the forwarded bearer token;
- "today/tomorrow/this week" are Core presets: the model never supplies the current date;
- guests never reach Core (no token is forwarded and the check below fails first).
"""

import re
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import Request

from homespace_ai.clients.gateway import GatewayApiClient

BASE_PATH = "/api/v1/appointments/ai"

ROLES = ("OWNER", "RENTER", "BOTH")
STATUSES = ("PENDING", "CONFIRMED", "REJECTED", "CANCELLED", "COMPLETED", "EXPIRED")
DATE_PRESETS = ("TODAY", "TOMORROW", "THIS_WEEK", "NEXT_WEEK", "THIS_WEEKEND", "THIS_MONTH")
AVAILABILITY_PRESETS = ("TODAY", "TOMORROW", "DAY_AFTER_TOMORROW")
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
WEEKS = ("THIS", "NEXT")
TIME_PHASES = ("UPCOMING", "IN_PROGRESS", "PAST")
SORTS = ("TIME_ASC", "TIME_DESC", "CREATED_DESC")


class AppointmentToolError(Exception):
    """kind: INVALID | UNAUTHENTICATED | NOT_FOUND | UNAVAILABLE. Never carries response bodies."""

    def __init__(self, kind: str, *, status_code: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status_code = status_code


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise AppointmentToolError("INVALID") from None


def _int(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise AppointmentToolError("INVALID")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise AppointmentToolError("INVALID")
    return value


def _enum(value: Any, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value.strip().upper() not in allowed:
        raise AppointmentToolError("INVALID")
    return value.strip().upper()


def _date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise AppointmentToolError("INVALID")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise AppointmentToolError("INVALID") from None
    return value


def _time(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise AppointmentToolError("INVALID")
    return value


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise AppointmentToolError("INVALID")
    return value.strip()


def _enum_list(value: Any, allowed: tuple[str, ...], limit: int) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > limit:
        raise AppointmentToolError("INVALID")
    return list(dict.fromkeys(_enum(item, allowed) for item in value))


_FILTERS: dict[str, Any] = {
    "role": lambda v: _enum(v, ROLES),
    "status": lambda v: _enum_list(v, STATUSES, 6),
    "datePreset": lambda v: _enum(v, DATE_PRESETS),
    "dateFrom": _date,
    "dateTo": _date,
    "timeFrom": _time,
    "timeTo": _time,
    "listingId": _uuid,
    "rescheduleRequested": _bool,
    "timePhase": lambda v: _enum_list(v, TIME_PHASES, 3),
    "q": lambda v: _text(v, 60),
}

# tool -> (path template, {argument: validator})
TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "summarize_my_appointments": ("/summary", dict(_FILTERS)),
    "search_my_appointments": ("/search", {
        **_FILTERS, "sort": lambda v: _enum(v, SORTS),
        "page": lambda v: _int(v, 1, 1000), "size": lambda v: _int(v, 1, 20)}),
    "get_appointment_detail": ("/items/{id}", {}),
    "get_listing_viewing_availability": ("/listings/{id}/availability", {
        "date": _date, "datePreset": lambda v: _enum(v, AVAILABILITY_PRESETS),
        "weekday": lambda v: _enum(v, WEEKDAYS), "week": lambda v: _enum(v, WEEKS)}),
}
ID_KEY = {"get_appointment_detail": "appointmentId", "get_listing_viewing_availability": "listingId"}


def normalize(name: str, arguments: Any) -> dict[str, Any]:
    """Validate a model-supplied or heuristic plan. Null means 'not provided' (strict-schema style)."""
    if name not in TOOLS or not isinstance(arguments, dict):
        raise AppointmentToolError("INVALID")
    _, validators = TOOLS[name]
    id_key = ID_KEY.get(name)
    allowed = set(validators) | ({id_key} if id_key else set())
    if set(arguments) - allowed:
        raise AppointmentToolError("INVALID")
    clean: dict[str, Any] = {}
    if id_key:
        clean[id_key] = _uuid(arguments.get(id_key))
    for key, validate in validators.items():
        value = arguments.get(key)
        if value is not None:
            clean[key] = validate(value)
    # Never let two ways of naming the same day coexist: Core would reject, and guessing is worse.
    if clean.get("datePreset") and (clean.get("dateFrom") or clean.get("dateTo") or clean.get("date")):
        raise AppointmentToolError("INVALID")
    if clean.get("weekday") and (clean.get("datePreset") or clean.get("date")):
        raise AppointmentToolError("INVALID")
    if clean.get("week") and not clean.get("weekday"):
        raise AppointmentToolError("INVALID")
    return clean


class AppointmentTools:
    def __init__(self, gateway: GatewayApiClient, request: Request, *, is_guest: bool = False) -> None:
        self.gateway = gateway
        self.request = request
        self.is_guest = is_guest

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.is_guest or not self.request.headers.get("authorization"):
            raise AppointmentToolError("UNAUTHENTICATED")
        clean = normalize(name, arguments)
        template, _ = TOOLS[name]
        id_key = ID_KEY.get(name)
        path = BASE_PATH + template.replace("{id}", clean.pop(id_key, "") if id_key else "")
        params = {k: ("true" if v is True else "false" if v is False else v) for k, v in clean.items()}
        response = await self._get(path, params)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and isinstance(data.get("result"), dict):
                return data["result"]
            raise AppointmentToolError("UNAVAILABLE", status_code=200)
        if response.status_code in (401, 403):
            raise AppointmentToolError("UNAUTHENTICATED", status_code=response.status_code)
        if response.status_code == 404:
            raise AppointmentToolError("NOT_FOUND", status_code=404)
        if response.status_code == 400:
            raise AppointmentToolError("INVALID", status_code=400)
        raise AppointmentToolError("UNAVAILABLE", status_code=response.status_code)

    async def _get(self, path: str, params: dict[str, Any]):
        from fastapi import HTTPException
        try:
            return await self.gateway.request("GET", path, request=self.request, params=params or None)
        except HTTPException as error:
            raise AppointmentToolError("UNAVAILABLE", status_code=error.status_code) from None
