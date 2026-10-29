"""Native provider tool calling for public listing lookup (contract v2); never generates SQL.

The model proposes one structured step. Application code validates it against the Core catalog,
merges it into the renter's stored criteria and Core compiles the query.
"""

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from homespace_ai.application.listing_catalog import SearchCatalog
from homespace_ai.application.listing_context import current_scope, History
from homespace_ai.application.listing_limits import max_results
from homespace_ai.clients.generative import GenerationUnavailableError, OpenAIResponsesClient
from homespace_ai.core.business_properties import nearby_default_radius_m
from homespace_ai.core.config import Settings

PLANNER_VERSION = 2
MISSING = ["LOCATION", "TARGET", "BUDGET", "CATEGORY", "CONFLICT", "DATE", "OCCUPANTS", "CONTEXT", "OTHER"]
NEARBY_GROUPS = ["EDUCATION", "HEALTHCARE", "SHOPPING", "FOOD_DRINK", "FINANCE", "TRANSPORT", "RECREATION", "SPORTS"]
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


@dataclass(frozen=True)
class ToolPlan:
    name: str
    arguments: dict[str, Any]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(schema)
    kind = result.get("type")
    result["type"] = [kind, "null"] if isinstance(kind, str) else [*kind, "null"]
    if "enum" in result:
        result["enum"] = [*result["enum"], None]
    return result


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _constraint_schema(catalog: SearchCatalog) -> dict[str, Any]:
    return _object({
        "field": {"type": "string", "enum": catalog.searchable()},
        "op": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "approx"]},
        "value": {"type": "string", "maxLength": 160, "description": "Single value; for in use values. Money in VND digits."},
        "values": _nullable({"type": "array", "items": {"type": "string", "maxLength": 60}, "maxItems": 20}),
        "strength": {"type": "string", "enum": ["hard", "preference"]},
    })


def search_parameters(catalog: SearchCatalog, cap: int) -> dict[str, Any]:
    constraint = _constraint_schema(catalog)
    kinds = [k["code"] for k in catalog.kinds]
    return _object({
        "constraints": {"type": "array", "items": constraint, "maxItems": 24},
        "alternatives": {"type": "array", "maxItems": 4, "items": _object({
            "constraints": {"type": "array", "items": constraint, "maxItems": 10}})},
        "location": _object({"ward": _nullable({"type": "string", "maxLength": 120}),
                             "city": _nullable({"type": "string", "maxLength": 120})}),
        "nationwide": {"type": "boolean"},
        "place": _object({"query": _nullable({"type": "string", "maxLength": 160}),
                          "radiusMeters": _nullable({"type": "integer", "minimum": 100, "maximum": 5000})}),
        "nearby": {"type": "array", "maxItems": 2, "items": _object({
            "kind": _nullable({"type": "string", "enum": kinds or ["NONE"]}),
            "group": _nullable({"type": "string", "enum": NEARBY_GROUPS}),
            "radiusMeters": _nullable({"type": "integer", "minimum": 100, "maximum": 5000})})},
        "context": _object({key: _nullable({"type": "integer", "minimum": 0, "maximum": 10000})
                            for key in ("occupants", "motorbikes", "cars", "electricityKwh", "waterM3")}),
        "sort": _nullable({"type": "string", "enum": catalog.sorts}),
        "limit": _nullable({"type": "integer", "minimum": 1, "maximum": cap}),
        "keyword": _nullable({"type": "string", "maxLength": 160}),
        "removeFields": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": 20},
        "moreResults": {"type": "boolean"},
        "withinDisplayed": {"type": "boolean"},
        "unsupported": {"type": "array", "maxItems": 6, "items": _object({
            "code": {"type": "string", "enum": [u["code"] for u in catalog.unsupported] or ["OTHER"]},
            "text": {"type": "string", "maxLength": 200}})},
    })


CLARIFY_PARAMETERS = _object({
    "missing": {"type": "array", "items": {"type": "string", "enum": MISSING}, "minItems": 1, "maxItems": 2},
    "question": {"type": "string", "maxLength": 400},
    "options": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 6},
})

DESCRIPTIONS = {
    "search_listings": "Search or refine current published listings with typed criteria. Core applies every hard criterion before LIMIT. Use withinDisplayed=true to filter only the displayed selection.",
    "ask_clarification": "Ask the customer for the one or two missing/ambiguous facts that change the result (area, exact campus, conflicting bounds, ambiguous date). Never a persona questionnaire.",
    "get_listing_detail": "Read all current public facts of ONE known displayed listing. Reading facts never changes search criteria.",
    "get_listing_details": "Read several known displayed listings to compare or recommend within that selection.",
    "get_listing_nearby_places": "Read cached nearby-place evidence for KNOWN displayed listing IDs (no provider scan). Use for which displayed room is nearer a place or has amenities around.",
}


def _instructions(catalog: SearchCatalog, settings: Settings, nearby_enabled: bool, today: date) -> str:
    kinds = "; ".join(f"{k['code']}={k.get('label', '')}" + (f" ({k['note']})" if k.get("note") else "") for k in catalog.kinds)
    unsupported = "; ".join(f"{u['code']}: {u.get('reason', '')}" for u in catalog.unsupported)
    cap = max_results(settings)
    rules = [
        "You plan ONE step for HomeSpace Vietnamese rental chat. Return at most one tool call.",
        f"TODAY={today.isoformat()} (request time, configured offset). Dates are YYYY-MM-DD.",
        "CURRENT_STATE holds the customer's confirmed criteria. Output only what this message adds or changes; "
        "anything you omit is KEPT. To drop a criterion put its field code (or location/target/keyword/nearby/context.<name>) in removeFields. "
        "'không cần thang máy' = removeFields amenity.ELEVATOR, never elevator=false. Reset only when the customer explicitly restarts.",
        "A short reply answers PENDING: fill exactly that slot (e.g. a ward name answers LOCATION) and keep all other criteria.",
        "Explicit conditions are hard. 'ưu tiên', 'nếu được', 'có thì tốt' are preference. 'khoảng/tầm X' is op=approx strength=preference, never a range.",
        "Budget: 'dưới X' lt, 'tối đa/không quá X' lte, 'trên X' gt, 'ít nhất/từ X trở lên' gte. Money in VND digits (3tr/3 củ = 3000000). "
        "Monthly rent uses rent.monthly_amount (per-person prices need context.occupants). 'Tổng chi phí mỗi tháng' uses cost.fixed_monthly_total "
        "with context occupants/motorbikes/cars; electricity/water usage only if the customer gave kWh/m³ (cost.estimated_monthly_total).",
        "N people: context.occupants=N and occupancy.max_occupants gte N. Vehicles go to context. Bedrooms/people are never the result count.",
        "Category only when named: phòng trọ/nhà trọ=ROOM, căn hộ/chung cư/studio=APARTMENT, nhà nguyên căn=HOUSE. 'phòng/chỗ ở' or a persona adds no category. "
        "Several types: category in [...]; type-specific conditions go to alternatives, each with its own category.",
        "Same charge/furniture/policy predicates share one row in Core. 'Ít nhất 2 giường còn tốt' = furnishing.BED.handover_condition gte GOOD AND furnishing.BED.total_quantity gte 2.",
        "Elevator=amenity.ELEVATOR; private WC=room.restroom_type PRIVATE; free hours=room.access_hours_type FLEXIBLE; private balcony=room.balcony_type PRIVATE; "
        "gym in building=amenity.GYM, gym nearby is a nearby need. Pets: policy.petPolicy.value ALLOWED (APPROVAL_REQUIRED is not certain permission; 'có thể xin phép' may use in [ALLOWED, APPROVAL_REQUIRED]).",
        "Location: ward/city names exactly as said (no guessed codes); never the account address. If no area, no named place and no explicit 'toàn quốc', call ask_clarification for LOCATION before searching. "
        "Do not ask budget/persona/amenities when the scope is already clear.",
        "Contradictory bounds in one message (e.g. từ 5 đến 4 triệu): output them exactly as said; never swap or drop.",
        "Reading a displayed listing ('phòng 2 có thang máy không?', 'phí xe phòng này') uses get_listing_detail and adds NO criteria. "
        "'Chỉ giữ phòng có thang máy' is a filter: search_listings withinDisplayed=true. 'Nếu thêm người thì sao' is hypothetical.",
        "'Xem thêm', '10 tin khác' = moreResults=true with unchanged criteria. Respect an explicit result count up to " + str(cap) + " via limit.",
        "Quiet/safe/airy/travel time/flooding have no data: put them in unsupported, never invent numeric or boolean filters. Unsupported: " + unsupported,
        "'Mình không hút thuốc' describes the renter, not a house rule filter.",
        "Greetings, HomeSpace help, contracts, payments: no tool call. Never invent listing IDs, place IDs or coordinates.",
    ]
    if nearby_enabled:
        rules.append(
            f"A named school/hospital/workplace/campus goes to place.query exactly as written (IUH, ĐH Công nghiệp); keyword never proves proximity. "
            f"Default radius {settings.nearby_default_radius_m} m; honor an explicit radius up to 5000. 'Gần nhất' to that place = sort target_distance. "
            "Generic 'gần siêu thị/bệnh viện/trường đại học' uses nearby.kind (exact type, not the broad group): " + kinds +
            ". SPORTS/gym nearby is not collected: report unknown, never absent. Distances are straight-line; no travel time.")
    else:
        rules.append("Nearby/distance lookup is disabled: do not use place or nearby; say location evidence is unavailable.")
    return "\n".join(rules) + "\nFIELDS (code ; kind ; operators ; unit ; categories ; enum ; label):\n" + catalog.prompt_text()


def state_summary(state: dict[str, Any] | None, catalog: SearchCatalog | None = None) -> dict[str, Any]:
    if not state:
        return {}
    def label(c):
        return {"field": c["field"], "op": c["op"], "value": c["value"], "strength": c["strength"]}
    return {"constraints": [label(c) for c in state["constraints"]],
            "alternatives": [[label(c) for c in group] for group in state["alternatives"]],
            "location": state.get("location"), "nationwide": state.get("nationwide"),
            "target": {"name": state["target"]["name"], "radiusMeters": state["target"]["radiusMeters"]} if state.get("target") else None,
            "nearby": state.get("nearby"), "context": state.get("context"), "sort": state.get("sort"),
            "unsupported": [u["code"] for u in state.get("unsupported") or []],
            "pending": {"type": state["pending"]["type"], "question": state["pending"]["question"],
                        "candidates": [c["name"] + (" — " + c["address"] if c.get("address") else "") for c in state["pending"]["candidates"]]}
            if state.get("pending") else None}


class ListingToolPlanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def plan(self, question: str, history: History, catalog: SearchCatalog,
                   state: dict[str, Any] | None, today: date) -> ToolPlan | None:
        if self.settings.generation_provider != "openai":
            raise GenerationUnavailableError("Listing tool planning requires OpenAI.", retryable=False)
        nearby_enabled = bool(getattr(self.settings, "nearby_query_enabled", False))
        cap = max_results(self.settings)
        tools = [
            {"type": "function", "name": "search_listings", "description": DESCRIPTIONS["search_listings"],
             "parameters": search_parameters(catalog, cap), "strict": True},
            {"type": "function", "name": "ask_clarification", "description": DESCRIPTIONS["ask_clarification"],
             "parameters": CLARIFY_PARAMETERS, "strict": True},
            {"type": "function", "name": "get_listing_detail", "description": DESCRIPTIONS["get_listing_detail"],
             "parameters": _object({"listingId": {"type": "string"}}), "strict": True},
        ]
        if cap >= 2:
            tools.append({"type": "function", "name": "get_listing_details", "description": DESCRIPTIONS["get_listing_details"],
                          "parameters": _object({"listingIds": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": cap}}),
                          "strict": True})
        if nearby_enabled:
            tools.append({"type": "function", "name": "get_listing_nearby_places", "description": DESCRIPTIONS["get_listing_nearby_places"],
                          "parameters": _object({
                              "listingIds": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": min(cap, 20)},
                              "radiusMeters": {"type": "integer", "minimum": 100, "maximum": 5000},
                              "groups": {"type": "array", "items": {"type": "string", "enum": NEARBY_GROUPS}},
                              "placeQuery": _nullable({"type": "string", "maxLength": 160})}), "strict": True})
        transport = OpenAIResponsesClient(self.settings)
        payload = transport.payload(self.settings.openai_tool_model, _instructions(catalog, self.settings, nearby_enabled, today),
                                    self._dialog(question, history, state, catalog), self.settings.openai_tool_max_output_tokens)
        payload.update({"tools": tools, "tool_choice": "auto", "parallel_tool_calls": False})
        data = await self._post(payload)
        if data.get("status") != "completed":
            raise GenerationUnavailableError("OpenAI did not complete tool planning.", retryable=False)
        try:
            output = data["output"]
            calls = [item for item in output if item.get("type") == "function_call"]
            if not calls:
                if any(part.get("type") == "refusal" for item in output for part in item.get("content", [])):
                    raise ValueError("Tool planner refused")
                text = " ".join(part.get("text", "") for item in output if item.get("type") == "message"
                                for part in item.get("content", []) if part.get("type") == "output_text").strip()
                return clarification_from_text(text)
            if len(calls) != 1:
                raise ValueError("Expected one tool call")
            return self._validated(calls[0]["name"], json.loads(calls[0]["arguments"]), question, history, nearby_enabled)
        except (KeyError, IndexError, TypeError, ValueError):
            raise GenerationUnavailableError("Malformed OpenAI tool call.", retryable=False) from None

    @staticmethod
    def _dialog(question: str, history: History, state: dict[str, Any] | None = None, catalog: SearchCatalog | None = None) -> str:
        scope = current_scope(history)
        lines = ["DISPLAYED_SELECTION (references only, NOT current facts): " + json.dumps(
                     [{"position": index, "listingId": value} for index, value in enumerate(scope, 1)]),
                 "CURRENT_STATE: " + json.dumps(state_summary(state, catalog), ensure_ascii=False, default=str)]
        # The window is for wording only; criteria older than it live in CURRENT_STATE.
        lines.extend(f"{item.get('role', 'user')}: {item.get('content', '')[:1200]}" for item in history[-8:])
        lines.append("user: " + question)
        return "\n".join(lines)

    @staticmethod
    def _validated(name: str, args: Any, question: str, history: History, nearby_enabled: bool) -> ToolPlan:
        if name == "find_listings_near_place":
            name = "search_listings"  # v1 name; the v2 search carries place/nearby itself.
        if name not in DESCRIPTIONS or not isinstance(args, dict):
            raise ValueError("Unknown listing tool")
        if name == "get_listing_nearby_places" and not nearby_enabled:
            raise ValueError("Nearby tools are disabled")
        if name == "search_listings":
            if not isinstance(args.get("constraints"), list):
                raise ValueError("Search constraints are required")
            return ToolPlan(name, args)
        if name == "ask_clarification":
            plan = clarification_from_text(args.get("question"), args.get("missing"), args.get("options"))
            if plan is None:
                raise ValueError("Empty clarification")
            return plan
        if name in {"get_listing_detail", "get_listing_details", "get_listing_nearby_places"}:
            known = set(current_scope(history))
            known.update(match.lower() for match in _UUID.findall(question))
            ids = [args.get("listingId")] if name == "get_listing_detail" else args.get("listingIds")
            if not isinstance(ids, list) or not ids or any(str(UUID(str(value))) not in known for value in ids):
                raise ValueError("Unknown listing reference")
            if name == "get_listing_nearby_places":
                radius = args.get("radiusMeters", nearby_default_radius_m())
                groups = args.get("groups")
                if type(radius) is not int or not 100 <= radius <= 5000:
                    raise ValueError("Invalid nearby radius")
                if not isinstance(groups, list) or len(groups) > 8 or len(set(groups)) != len(groups) or any(g not in NEARBY_GROUPS for g in groups):
                    raise ValueError("Unknown nearby group")
        return ToolPlan(name, args)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await OpenAIResponsesClient(self.settings).create(payload, task="listing_tool_plan")


def clarification_from_text(text: Any, missing: Any = None, options: Any = None) -> ToolPlan | None:
    """Keep a meaningful model question, validated: bounded, no links, IDs or markup."""
    if not isinstance(text, str):
        return None
    clean = re.sub(r"https?://\S+|/rent/\S+", "", text)
    clean = _UUID.sub("", clean)
    clean = re.sub(r"[`*_#>|{}\[\]]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()[:400]
    if len(clean) < 8 or "?" not in clean:
        return None
    missing = [m for m in (missing or ["OTHER"]) if m in MISSING][:2] or ["OTHER"]
    options = [re.sub(r"\s+", " ", str(o)).strip()[:80] for o in (options or []) if isinstance(o, str) and o.strip()][:6]
    return ToolPlan("ask_clarification", {"question": clean, "missing": missing, "options": options})
