"""Read-only contract tools for the signed-in account.

Safety properties enforced here (not left to the model):
- a fixed table of tools -> fixed Core paths; the model cannot choose a path, method, host or header;
- strict argument whitelists (enum / range / UUID / date); unknown keys are rejected;
- the caller identity is never an argument: Core derives it from the forwarded bearer token;
- guests never reach Core (no token is forwarded and the check below fails first).
"""

import re
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import Request

from homespace_ai.clients.gateway import GatewayApiClient

BASE_PATH = "/api/v1/contracts/ai"

ROLES = ("ANY", "LANDLORD", "TENANT")
STATUSES = ("DRAFT", "PENDING_REVIEW", "LANDLORD_SIGNATURE_PENDING", "TENANT_SIGNATURE_PENDING",
            "ACTIVE", "EXPIRED", "CLOSED", "TERMINATED", "CANCELLED")
LEASE_PHASES = ("NOT_SIGNED", "NOT_STARTED", "IN_LEASE", "LEASE_ENDED", "CLOSED", "TERMINATED", "CANCELLED", "UNKNOWN")
SORTS = ("CREATED_DESC", "LEASE_END_ASC", "LEASE_END_DESC")
TERM_GROUPS = ("LEASE", "PRICING", "CHARGES", "EQUIPMENT", "AMENITIES", "POLICIES", "SPECIAL_TERMS", "PROPERTY", "OCCUPANCY")
INVOICE_FILTERS = ("ALL", "OUTSTANDING", "PAID")


class ContractToolError(Exception):
    """kind: INVALID | UNAUTHENTICATED | NOT_FOUND | UNAVAILABLE. Never carries response bodies."""

    def __init__(self, kind: str, *, status_code: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status_code = status_code


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ContractToolError("INVALID") from None


def _int(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ContractToolError("INVALID")
    return value


def _enum(value: Any, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value.strip().upper() not in allowed:
        raise ContractToolError("INVALID")
    return value.strip().upper()


def _date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ContractToolError("INVALID")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ContractToolError("INVALID") from None
    return value


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ContractToolError("INVALID")
    return value.strip()


def _enum_list(value: Any, allowed: tuple[str, ...], limit: int) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > limit:
        raise ContractToolError("INVALID")
    return list(dict.fromkeys(_enum(item, allowed) for item in value))


# tool -> (path template or None for root endpoints, {argument: validator})
TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "summarize_my_contracts": ("/summary", {
        "role": lambda v: _enum(v, ROLES), "expiringWithinDays": lambda v: _int(v, 1, 365)}),
    "search_my_contracts": ("/search", {
        "role": lambda v: _enum(v, ROLES), "status": lambda v: _enum_list(v, STATUSES, 9),
        "leasePhase": lambda v: _enum(v, LEASE_PHASES), "expiringWithinDays": lambda v: _int(v, 1, 365),
        "q": lambda v: _text(v, 80), "sort": lambda v: _enum(v, SORTS),
        "page": lambda v: _int(v, 1, 1000), "size": lambda v: _int(v, 1, 20)}),
    "get_contract_detail": ("/items/{id}", {}),
    "get_contract_terms": ("/items/{id}/terms", {
        "groups": lambda v: _enum_list(v, TERM_GROUPS, 9), "asOf": _date}),
    "get_contract_amendments": ("/items/{id}/amendments", {}),
    "get_contract_lifecycle": ("/items/{id}/lifecycle", {}),
    "get_contract_financial_summary": ("/items/{id}/financial", {
        "invoices": lambda v: _enum(v, INVOICE_FILTERS), "limit": lambda v: _int(v, 1, 36)}),
}
BY_CONTRACT = {name for name, (path, _) in TOOLS.items() if "{id}" in path}


def normalize(name: str, arguments: Any) -> dict[str, Any]:
    """Validate a model-supplied or heuristic plan. Null means 'not provided' (strict-schema style)."""
    if name not in TOOLS or not isinstance(arguments, dict):
        raise ContractToolError("INVALID")
    _, validators = TOOLS[name]
    allowed = set(validators) | ({"contractId"} if name in BY_CONTRACT else set())
    if set(arguments) - allowed:
        raise ContractToolError("INVALID")
    clean: dict[str, Any] = {}
    if name in BY_CONTRACT:
        clean["contractId"] = _uuid(arguments.get("contractId"))
    for key, validate in validators.items():
        value = arguments.get(key)
        if value is not None:
            clean[key] = validate(value)
    return clean


class ContractTools:
    def __init__(self, gateway: GatewayApiClient, request: Request, *, is_guest: bool = False) -> None:
        self.gateway = gateway
        self.request = request
        self.is_guest = is_guest

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.is_guest or not self.request.headers.get("authorization"):
            raise ContractToolError("UNAUTHENTICATED")
        clean = normalize(name, arguments)
        template, _ = TOOLS[name]
        path = BASE_PATH + template.replace("{id}", clean.pop("contractId", ""))
        response = await self._get(path, clean)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and isinstance(data.get("result"), dict):
                return data["result"]
            raise ContractToolError("UNAVAILABLE", status_code=200)
        if response.status_code in (401, 403):
            raise ContractToolError("UNAUTHENTICATED", status_code=response.status_code)
        if response.status_code == 404:
            raise ContractToolError("NOT_FOUND", status_code=404)
        if response.status_code == 400:
            raise ContractToolError("INVALID", status_code=400)
        raise ContractToolError("UNAVAILABLE", status_code=response.status_code)

    async def _get(self, path: str, params: dict[str, Any]):
        from fastapi import HTTPException
        try:
            return await self.gateway.request("GET", path, request=self.request, params=params or None)
        except HTTPException as error:
            raise ContractToolError("UNAVAILABLE", status_code=error.status_code) from None
