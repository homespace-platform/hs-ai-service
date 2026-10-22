"""Native provider tool calling for public listing lookup; never generates SQL."""

import json
import re
from dataclasses import dataclass
from copy import deepcopy
from typing import Any
from uuid import UUID

from homespace_ai.clients.generative import GenerationUnavailableError, OpenAIResponsesClient
from homespace_ai.core.config import Settings
from homespace_ai.application.listing_context import current_scope, History
from homespace_ai.application.listing_location import resolve_location_filters
from homespace_ai.application.vietnamese_text import fold_text
from homespace_ai.application.listing_limits import max_results, search_limit


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
        "keyword": {"type": ["string", "null"]},
    },
    "required": ["filters"],
}

# "gần <địa danh>" where the place is a landmark (university, hospital, market, metro...) rather than an
# administrative unit. Core stores landmarks only in title/description/address text, so use keyword search.
_NEAR = re.compile(r"\b(?:gần|cạnh|sát|quanh|khu vực)\s+(?!phường\b|xã\b|đặc khu\b|quận\b|huyện\b|thành phố\b|tỉnh\b)"
                   r"(?P<place>(?:[^,;?!\n.]|\.(?=\S)){3,80})", re.IGNORECASE)
_PROVINCE_TAIL = re.compile(r"\s*(?:,|\b)\s*(?:thành phố|tp\.?|tỉnh)?\s*(?:hồ chí minh|hcm|tp\.?\s*hcm|sài gòn)\s*$",
                            re.IGNORECASE)
_TRAILING_NEEDS = re.compile(r"\s+(?:giá|dưới|trên|khoảng|từ|có|không|với|và|để|cho|ở|tầm|diện tích)\b.*$",
                             re.IGNORECASE)

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
    "search_listings": "Find NEW current published rental listings using exact typed field predicates. Do not use for choosing among the displayed selection; read the known IDs instead.",
    "get_listing_detail": "Read all current public fields of one listing by its UUID, including prices, charges, amenities and furnishing assets.",
    "get_listing_details": "Read known listing UUIDs together within the configured page limit for follow-up comparison, recommendation, or evaluating changed preferences WITHIN the displayed selection. Re-read facts; do not run a new search.",
}


def _search_parameters(fields: dict[str, str]) -> dict[str, Any]:
    parameters = deepcopy(_SEARCH_PARAMETERS)
    parameters["properties"]["filters"]["items"]["properties"]["field"]["enum"] = sorted(fields)
    return parameters


class ListingToolPlanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def plan(self, question: str, history: History, fields: dict[str, str]) -> ToolPlan | None:
        if self.settings.generation_provider != "openai":
            raise GenerationUnavailableError("Listing tool planning requires OpenAI.", retryable=False)
        plan = await self._openai(question, history, fields)
        plan = self._normalize_search(question, plan, fields, history)
        if plan is not None and plan.name == 'search_listings':
            arguments = {**plan.arguments, 'filters': resolve_location_filters(plan.arguments['filters'], question, history, fields),
                         'limit': search_limit(question, self.settings)}
            return ToolPlan(plan.name, arguments)
        return plan

    @staticmethod
    def _normalize_search(question: str, plan: ToolPlan | None, fields: dict[str, str],
                          history: History | None = None) -> ToolPlan | None:
        if plan is None or plan.name != "search_listings":
            return plan
        raw_filters = plan.arguments.get("filters")
        if not isinstance(raw_filters, list):
            raise GenerationUnavailableError("Malformed listing search filters.", retryable=False)

        category_text = fold_text(question)
        mentioned = []
        if re.search(r"nha nguyen can", category_text):
            mentioned.append("HOUSE")
        if re.search(r"can ho|chung cu|studio", category_text):
            mentioned.append("APARTMENT")
        if re.search(r"phong tro|nha tro|\bphong\b(?!\s*(?:ngu|tam|khach|bep|ve sinh|\d+\s*(?:pn|wc)))", category_text):
            mentioned.append("ROOM")
        # One explicit type is authoritative. Several types ("phòng trọ hoặc căn hộ") or none
        # ("chỗ ở", "nhà") must not be forced into a single category, nor keep a model guess.
        category = mentioned[0] if len(mentioned) == 1 else None
        if len(mentioned) > 1:  # no mention: keep the model's category, which may be inherited from history
            raw_filters = [item for item in raw_filters
                           if not (isinstance(item, dict) and item.get("field") == "category")]

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
            occupants = re.search(r"(?:ở|cho|nhóm)\s+(\d+)\s*(?:người|mình)\b", question, re.IGNORECASE)
            if field.endswith(".max_occupants") and occupants and op == "eq" and not re.search(
                    r"tối đa|sức chứa|chỉ cho", question, re.IGNORECASE):
                value, op = occupants[1], "gte"
            if field in {"ward", "city"} and op == "eq":
                prefixes = r"Phường|Xã|Đặc khu" if field == "ward" else r"Thành phố|Tỉnh"
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

        keyword = plan.arguments.get("keyword")
        keyword = keyword.strip() if isinstance(keyword, str) else ""
        if not keyword:
            near = _NEAR.search(question)
            if near:
                keyword = _TRAILING_NEEDS.sub("", near["place"]).strip()
                keyword = _PROVINCE_TAIL.sub("", keyword).strip(" -,")
                if len(keyword) < 3:
                    keyword = ""
        if keyword:
            # A landmark is not an administrative unit: ward/street guesses derived from it
            # silently exclude the listings that actually mention the landmark.
            def mentioned(value: str) -> bool:
                bare = re.sub(r'^(?:phuong|xa|dac khu)\s+', '', fold_text(value))
                return (fold_text(value) not in fold_text(keyword)
                        and bool(re.search(r'\b' + re.escape(bare) + r'\b', fold_text(question))))
            normalized = [item for item in normalized
                          if not (item["field"] in {"ward", "street", "display"}
                                  and not mentioned(item["value"]))
                          and not (item["field"] in {"title", "description"} and item["op"] == "contains"
                                   and keyword.lower() in item["value"].lower())]
        arguments = {**plan.arguments, "filters": normalized}
        if keyword:
            arguments["keyword"] = keyword[:160]
        else:
            arguments.pop("keyword", None)
        return ToolPlan(plan.name, arguments)

    @staticmethod
    def _instructions(fields: dict[str, str]) -> str:
        catalog = ", ".join(f"{name}:{kind}" for name, kind in sorted(fields.items()))
        return (
            "You route HomeSpace Vietnamese messages. Call search_listings to find NEW rental listings. "
            "For details of one known listing call get_listing_detail. For comparing, recommending, "
            "ranking or changed preferences within the displayed selection, call get_listing_details "
            "with its known UUIDs; get_listing_detail if the selection has only one listing. "
            "'Ở một mình, nên chọn phòng nào?', 'rẻ nhất trong các phòng trên' and 'có phí gửi xe "
            "thì chọn phòng nào?' are listing recommendations, NOT RAG and NOT new searches. "
            "Only search elsewhere when the user requests more/new listings or a new area/category. "
            "The DISPLAYED_SELECTION preserves ordinal references, e.g. phòng 3 is position 3, "
            "not the third item in a reordered search. Never include undisplayed IDs. "
            "For general HomeSpace help, policies, contracts, payments, or greetings, do not call a tool; "
            "those questions use RAG. Never invent a listing ID, location code or field. "
            "Use only these field names; field operators are exact, contains, or numeric/date comparisons. "
            "For a user's changed preference, use the latest preference, not an obsolete one. "
            "For N occupants use max_occupants >= N, never equality: a room for up to 3 can host 1. "
            "'Chi phí rẻ nhất' is a ranking objective, not a made-up budget filter. "
            "For NEW searches requesting cheapest rent use sort=price_asc, not sort=newest. "
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
            "Push ALL explicit criteria (location, budget, area, occupants, amenities, furnishing) into filters; "
            "the database filters BEFORE LIMIT. Never fetch a whole ward for the model to filter. "
            "Use location NAMES, not guessed IDs; application code resolves names/aliases against Vietmap admin_new. "
            "A legacy quan/huyen is not the new phuong with a similar name; clarification is required. "
            "LANDMARKS: when the user wants a place NEAR a landmark (university, hospital, market, metro, "
            "company, park, e.g. 'gần ĐH Quốc gia TP.HCM Thủ Đức') put the landmark text, exactly as the user wrote it "
            "without 'gần' and without city/province, into the 'keyword' argument (otherwise null). It is matched against "
            "title, description and address. Do NOT convert a landmark into ward, street or any administrative "
            "filter, and do not guess a ward. Use ward/city filters only for Phường/Xã/Thành phố/Tỉnh the user named. "
            "UNDERSTAND THE NEED, not keywords: 'chỗ ở', 'chỗ thuê', 'kiếm nơi ở' mean rental search with NO category filter; "
            "'phòng trọ/nhà trọ' = ROOM; 'căn hộ/chung cư/studio' = APARTMENT; 'nhà nguyên căn/nhà riêng' = HOUSE. "
            "If the user accepts several types ('phòng trọ hoặc căn hộ'), do not filter category. "
            "Budgets: '3 triệu', '3tr', '3 củ' = 3000000 VND/month; 'dưới/tối đa/không quá' = lte, 'trên/ít nhất' = gte, "
            "'khoảng/tầm X' = a range from 0.8X to 1.2X, never equality. 'Rộng rãi' or 'thoáng' alone is not a numeric filter. "
            "A street name belongs in the street filter (e.g. 'Đường Nguyễn Oanh'), a ward in ward; never mix them. "
            "Do not call a tool when the user only greets, asks how HomeSpace works, or the message has no rental need. "
            "Return at most one tool call. Available fields: " + catalog
        )

    @staticmethod
    def _dialog(question: str, history: History) -> str:
        recent = history[-8:]
        scope = current_scope(history)
        lines = ["DISPLAYED_SELECTION (references only, NOT current facts): " + json.dumps(
            [{"position": index, "listingId": value} for index, value in enumerate(scope, 1)])]
        lines.extend(f"{item.get('role', 'user')}: {item.get('content', '')[:1200]}" for item in recent)
        lines.append("user: " + question)
        return "\n".join(lines)

    async def _openai(self, question: str, history: History, fields: dict[str, str]) -> ToolPlan | None:
        tools = []
        for name, description in _DESCRIPTIONS.items():
            if name == 'get_listing_details' and max_results(self.settings) < 2:
                continue
            parameters = (_search_parameters(fields) if name == "search_listings" else
                          deepcopy(_DETAIL_PARAMETERS if name == "get_listing_detail" else _MULTI_DETAIL_PARAMETERS))
            # Responses strict schemas require every property and reject undeclared properties.
            parameters["required"] = list(parameters["properties"])
            parameters["additionalProperties"] = False
            if name == "search_listings":
                parameters["properties"]["filters"]["items"]["additionalProperties"] = False
                parameters["properties"]["filters"]["maxItems"] = 24
                parameters["properties"]["limit"].update({"minimum": 1, "maximum": max_results(self.settings)})
                parameters["properties"]["keyword"] = {"type": ["string", "null"], "maxLength": 160}
            elif name == "get_listing_details":
                parameters["properties"]["listingIds"].update({"minItems": 2, "maxItems": max_results(self.settings)})
            tools.append({"type": "function", "name": name, "description": description,
                          "parameters": parameters, "strict": True})
        transport = OpenAIResponsesClient(self.settings)
        payload = transport.payload(
            self.settings.openai_tool_model,
            self._instructions(fields) + f" When unspecified use sort=newest and limit=5. Respect an explicit requested count, up to {max_results(self.settings)} per request.",
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
            plan = self._validated(calls[0]["name"], json.loads(calls[0]["arguments"]))
            if plan.name != "search_listings":
                known_ids = set(current_scope(history))
                known_ids.update(match.lower() for match in re.findall(
                    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", question))
                ids = ([plan.arguments["listingId"]] if plan.name == "get_listing_detail"
                       else plan.arguments["listingIds"])
                if not ids or any(str(UUID(str(value))) not in known_ids for value in ids):
                    raise ValueError("Unknown listing reference")
            return plan
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
