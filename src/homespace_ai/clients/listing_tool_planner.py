"""Native provider tool calling for public listing lookup; never generates SQL."""

import json
import re
from dataclasses import dataclass
from copy import deepcopy
from typing import Any

from homespace_ai.clients.generative import GenerationUnavailableError, OpenAIResponsesClient
from homespace_ai.core.config import Settings


@dataclass(frozen=True)
class ToolPlan:
    name: str
    arguments: dict[str, Any]


_SEARCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filters": {"type": "array", "items": {"type": "object", "properties": {
            "field": {"type": "string"},
            "op": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "contains"]},
            "value": {"type": "string"},
        }, "required": ["field", "op", "value"]}},
        "sort": {"type": "string", "enum": ["newest", "price_asc", "price_desc", "area_asc", "area_desc"]},
        "limit": {"type": "integer"},
    },
    "required": ["filters"],
}

_DETAIL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"listingId": {"type": "string"}},
    "required": ["listingId"],
}

_MULTI_DETAIL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"listingIds": {"type": "array", "items": {"type": "string"}}},
    "required": ["listingIds"],
}

_DESCRIPTIONS = {
    "search_listings": "Search current published rental listings using exact typed field predicates. Use for finding, filtering, counting, comparing, or follow-up questions about listings.",
    "get_listing_detail": "Read all current public fields of one listing by its UUID, including prices, charges, amenities and furnishing assets.",
    "get_listing_details": "Read two to five known listing UUIDs together to compare their current public fields. Use for follow-up comparisons of previously returned listings.",
}


def _search_parameters(fields: dict[str, str]) -> dict[str, Any]:
    parameters = deepcopy(_SEARCH_PARAMETERS)
    parameters["properties"]["filters"]["items"]["properties"]["field"]["enum"] = sorted(fields)
    return parameters


class ListingToolPlanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def plan(self, question: str, history: list[dict[str, str]], fields: dict[str, str]) -> ToolPlan | None:
        if self.settings.generation_provider != "openai":
            raise GenerationUnavailableError("Listing tool planning requires OpenAI.", retryable=False)
        plan = await self._openai(question, history, fields)
        return self._normalize_search(question, plan, fields, history)

    @staticmethod
    def _normalize_search(question: str, plan: ToolPlan | None, fields: dict[str, str],
                          history: list[dict[str, str]] | None = None) -> ToolPlan | None:
        if plan is None or plan.name != "search_listings":
            return plan
        raw_filters = plan.arguments.get("filters")
        if not isinstance(raw_filters, list):
            raise GenerationUnavailableError("Malformed listing search filters.", retryable=False)

        category = None
        if re.search(r"nhà nguyên căn", question, re.IGNORECASE):
            category = "HOUSE"
        elif re.search(r"căn hộ|chung cư", question, re.IGNORECASE):
            category = "APARTMENT"
        elif re.search(r"phòng trọ|nhà trọ|\bphòng\b(?!\s*(?:ngủ|tắm|khách|bếp|vệ sinh))", question, re.IGNORECASE):
            category = "ROOM"

        parking_mentioned = bool(re.search(r"gửi xe|để xe|đỗ xe", question, re.IGNORECASE))
        parking_free = bool(re.search(r"miễn phí|không (?:mất|thu|tính) phí", question, re.IGNORECASE))
        parking_paid = bool(re.search(r"có phí|thu phí|trả phí|tính phí", question, re.IGNORECASE)) and not parking_free
        parking_forbidden = bool(re.search(r"không (?:có|cho|hỗ trợ) (?:chỗ )?(?:gửi|để|đỗ) xe", question, re.IGNORECASE))
        balcony_mentioned = bool(re.search(r"ban công", question, re.IGNORECASE))
        balcony_absent = bool(re.search(r"không (?:có )?ban công", question, re.IGNORECASE))
        normalized: list[dict[str, str]] = []

        for item in raw_filters:
            if not isinstance(item, dict):
                raise GenerationUnavailableError("Malformed listing filter.", retryable=False)
            field, op, value = item.get("field"), item.get("op"), item.get("value")
            if not isinstance(field, str) or not isinstance(op, str) or value is None:
                raise GenerationUnavailableError("Malformed listing filter.", retryable=False)
            value = str(value).strip()
            if field == "category" and category:
                value, op = category, "eq"
            if field in {"ward_name", "province_name"} and op == "eq":
                prefixes = r"Phường|Xã|Đặc khu" if field == "ward_name" else r"Thành phố|Tỉnh"
                if not re.match(rf"(?:{prefixes})\s", value, re.IGNORECASE):
                    mention = re.search(rf"\b({prefixes})\s+{re.escape(value)}\b", question, re.IGNORECASE)
                    if not mention:
                        for previous in reversed(history or []):
                            if previous.get("role") == "user":
                                mention = re.search(rf"\b({prefixes})\s+{re.escape(value)}\b",
                                                    previous.get("content", ""), re.IGNORECASE)
                                if mention:
                                    break
                    if mention:
                        value = mention.group(0)
                    else:
                        # Core stores full administrative names; equality with a bare name is a false negative.
                        op = "contains"
            # Do not narrow a generic balcony request to PRIVATE merely because the model guessed it.
            if field == "room.balcony_type" and balcony_mentioned:
                if re.search(r"ban công riêng", question, re.IGNORECASE):
                    op, value = "eq", "PRIVATE"
                elif re.search(r"ban công chung", question, re.IGNORECASE):
                    op, value = "eq", "SHARED"
                else:
                    field, op, value = "room.has_balcony", "eq", str(not balcony_absent).lower()
            # Presence of parking must not silently become a paid/free preference.
            if parking_mentioned and not parking_free and not parking_paid and field.startswith("charge.MOTORBIKE_PARKING."):
                continue
            if parking_mentioned and field == "room.parking_policy" and not parking_free and not parking_paid:
                field, op, value = "room.parking_policy", ("eq" if parking_forbidden else "ne"), "NONE"
            if field not in fields or op not in {"eq", "ne", "gt", "gte", "lt", "lte", "contains"}:
                raise GenerationUnavailableError("Unsupported listing filter from model.", retryable=False)
            normalized.append({"field": field, "op": op, "value": value})

        if category and "category" in fields and not any(item["field"] == "category" for item in normalized):
            normalized.append({"field": "category", "op": "eq", "value": category})
        if parking_mentioned and category == "ROOM" and "room.parking_policy" in fields:
            normalized = [item for item in normalized if item["field"] != "room.parking_policy"]
            normalized.append({"field": "room.parking_policy", "op": "eq" if parking_forbidden or parking_free or parking_paid else "ne",
                               "value": "NONE" if parking_forbidden else "FREE" if parking_free else "PAID" if parking_paid else "NONE"})
        if len(normalized) > 24:
            raise GenerationUnavailableError("Too many listing filters.", retryable=False)
        return ToolPlan(plan.name, {**plan.arguments, "filters": normalized})

    @staticmethod
    def _instructions(fields: dict[str, str]) -> str:
        catalog = ", ".join(f"{name}:{kind}" for name, kind in sorted(fields.items()))
        return (
            "You route HomeSpace Vietnamese messages. Call search_listings for any request about current "
            "rental listings, their fields, costs, amenities, furnishings, availability, or comparisons. "
            "Call get_listing_detail when one listing UUID is known from this conversation. "
            "Call get_listing_details for comparisons of previously returned listings with known UUIDs. "
            "For general HomeSpace help, policies, contracts, payments, or greetings, do not call a tool; "
            "those questions use RAG. Never invent a listing ID, location code or field. "
            "Use only these field names; field operators are exact, contains, or numeric/date comparisons. "
            "For a user's changed preference, use the latest preference, not an obsolete one. "
            "Boolean false is a real filter, not an omitted filter. "
            "For charge fields, check amount and included_in_rent separately. "
            "Category values are HOUSE, APARTMENT, ROOM. Room parking_policy values are NONE, FREE, PAID; "
            "FREE means no parking fee, PAID means a charge applies, NONE means no parking. "
            "Room restroom_type and kitchen_type use PRIVATE or SHARED (kitchen may also be NONE). "
            "Room balcony_type uses PRIVATE, SHARED, NONE. Furnishing status uses UNFURNISHED, BASIC, "
            "PARTIALLY_FURNISHED, FULLY_FURNISHED, LUXURY. Viewing slots use MORNING, AFTERNOON, EVENING. "
            "For parking-fee questions filter charge.MOTORBIKE_PARKING.included_in_rent rather than just amenity.PARKING. "
            "Keep full administrative names such as Phường Phú Lợi and Thành phố Hồ Chí Minh. "
            "A request for parking availability does not imply paid or free parking. "
            "A request for any balcony means room.has_balcony=true, not a guessed PRIVATE balcony type. "
            "Never add a condition the user did not request. "
            "Return at most one tool call. Available fields: " + catalog
        )

    @staticmethod
    def _dialog(question: str, history: list[dict[str, str]]) -> str:
        recent = history[-8:]
        lines = [f"{item.get('role', 'user')}: {item.get('content', '')[:1200]}" for item in recent]
        lines.append("user: " + question)
        return "\n".join(lines)

    async def _openai(self, question: str, history: list[dict[str, str]], fields: dict[str, str]) -> ToolPlan | None:
        tools = []
        for name, description in _DESCRIPTIONS.items():
            parameters = (_search_parameters(fields) if name == "search_listings" else
                          deepcopy(_DETAIL_PARAMETERS if name == "get_listing_detail" else _MULTI_DETAIL_PARAMETERS))
            # Responses strict schemas require every property and reject undeclared properties.
            parameters["required"] = list(parameters["properties"])
            parameters["additionalProperties"] = False
            if name == "search_listings":
                parameters["properties"]["filters"]["items"]["additionalProperties"] = False
                parameters["properties"]["filters"]["maxItems"] = 24
                parameters["properties"]["limit"].update({"minimum": 1, "maximum": 5})
            elif name == "get_listing_details":
                parameters["properties"]["listingIds"].update({"minItems": 2, "maxItems": 5})
            tools.append({"type": "function", "name": name, "description": description,
                          "parameters": parameters, "strict": True})
        transport = OpenAIResponsesClient(self.settings)
        payload = transport.payload(
            self.settings.openai_tool_model,
            self._instructions(fields) + " When unspecified use sort=newest and limit=5.",
            self._dialog(question, history), self.settings.openai_tool_max_output_tokens,
        )
        payload.update({"tools": tools, "tool_choice": "auto", "parallel_tool_calls": False})
        data = await self._post(payload)
        if data.get("status") != "completed":
            raise GenerationUnavailableError("OpenAI did not complete tool planning.", retryable=False)
        try:
            calls = [item for item in data["output"] if item.get("type") == "function_call"]
            if not calls:
                # Refusals are not a RAG routing decision.
                if any(part.get("type") == "refusal" for item in data["output"]
                       for part in item.get("content", [])):
                    raise ValueError("Tool planner refused")
                return None
            if len(calls) != 1:
                raise ValueError("Expected one tool call")
            return self._validated(calls[0]["name"], json.loads(calls[0]["arguments"]))
        except (KeyError, IndexError, TypeError, ValueError):
            raise GenerationUnavailableError("Malformed OpenAI tool call.", retryable=False) from None

    @staticmethod
    def _validated(name: str, args: Any) -> ToolPlan:
        if name not in _DESCRIPTIONS or not isinstance(args, dict):
            raise ValueError("Unknown listing tool")
        if name == "search_listings" and not isinstance(args.get("filters"), list):
            raise ValueError("Search filters are required")
        if name == "get_listing_detail" and not isinstance(args.get("listingId"), str):
            raise ValueError("Listing ID is required")
        if name == "get_listing_details" and not isinstance(args.get("listingIds"), list):
            raise ValueError("Listing IDs are required")
        return ToolPlan(name=name, arguments=args)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await OpenAIResponsesClient(self.settings).create(payload, task="listing_tool_plan")
