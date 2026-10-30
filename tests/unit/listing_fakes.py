"""Shared offline fakes for the listing flow: Core v2 catalog/search, planner and generator.

The catalog is exported from Core's seeded catalog (AiCatalogContractTest), so field codes,
operators and enums are the ones Core compiles. No network, model or provider is reachable.
"""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from homespace_ai.application.use_cases.listing_ask import ListingAsk
from homespace_ai.clients.generative import ListingGeneratedAnswer
from homespace_ai.clients.listing_tool_planner import ToolPlan
from homespace_ai.tools.listings import ListingToolError

CATALOG = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "core_catalog_v2.json").read_text(encoding="utf-8"))
ID = "00000000-0000-0000-0000-000000000001"
IDS = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 11)]
FIXED_NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)


def c(field, op, value, strength="hard", values=None):
    return {"field": field, "op": op, "value": str(value), "values": values, "strength": strength}


def search(*constraints, **extra):
    args = {"constraints": list(constraints), "alternatives": [], "location": {"ward": None, "city": None},
            "nationwide": False, "place": {"query": None, "radiusMeters": None}, "nearby": [],
            "context": {k: None for k in ("occupants", "motorbikes", "cars", "electricityKwh", "waterM3")},
            "sort": None, "limit": None, "keyword": None, "removeFields": [], "moreResults": False,
            "withinDisplayed": False, "unsupported": []}
    for key, value in extra.items():
        if key in {"ward", "city"}:
            args["location"][key] = value
        elif key == "place":
            args["place"]["query"] = value
        elif key == "radius":
            args["place"]["radiusMeters"] = value
        else:
            args[key] = value
    return ToolPlan("search_listings", args)


def room(listing_id, **overrides):
    item = {"id": listing_id, "title": "Phòng thử nghiệm", "category": "ROOM", "status": "PUBLISHED", "active": True,
            "areaM2": 20, "pricing": {"amount": 3000000, "unit": "ROOM_MONTH"},
            "address": {"ward": "Phường Hạnh Thông", "city": "Thành phố Hồ Chí Minh"},
            "roomDetail": {"parkingPolicy": "PAID", "maxOccupants": 2},
            "charges": [{"chargeType": "MOTORBIKE_PARKING", "amount": 80000, "includedInRent": False}]}
    item.update(overrides)
    return item


class FakeTools:
    def __init__(self, ids=None, details=None, places=None):
        self.ids = list(ids) if ids is not None else [ID]
        self.details = details or {}
        self.places = places or {}
        self.payloads, self.read, self.place_queries, self.nearby_calls = [], [], [], []
        self.catalog_calls = 0
        self.max_results = 20
        self.gone: set[str] = set()
        self.reject = None
        self.unknown_total = 0
        self.context_missing: list[str] = []
        self.evidence: list[dict] = []
        self.summaries = None
        self.value_calls = []
        self.value_suggestion = None
        self.value_boundary = {"candidateIds": [], "budgetField": None, "budgetCeiling": None, "maxExtraMonthly": 500000}

    async def catalog(self):
        self.catalog_calls += 1
        return deepcopy(CATALOG)

    @property
    def search_calls(self):
        return len(self.payloads)

    def select(self, payload):
        scope = payload.get("scopeIds")
        return [i for i in self.ids if (scope is None or i in scope) and i not in (payload.get("excludeIds") or [])]

    async def search_v2(self, payload):
        self.payloads.append(deepcopy(payload))
        if self.reject is not None:
            raise self.reject
        ids = self.select(payload)
        total = len([i for i in self.ids if payload.get("scopeIds") is None or i in payload["scopeIds"]])
        return {"listingIds": ids[:payload["limit"]], "total": total, "unknownTotal": self.unknown_total,
                "contextMissing": list(self.context_missing), "evidence": [e for e in self.evidence if e["listingId"] in ids],
                "readAt": "2026-10-29T03:00:00Z", "catalogVersion": CATALOG["catalogVersion"]}

    async def detail(self, listing_id):
        self.read.append(listing_id)
        if listing_id in self.gone:
            raise ListingToolError("Listing is no longer public")
        return deepcopy(self.details.get(listing_id) or room(listing_id))

    async def resolve_place(self, query):
        self.place_queries.append(query)
        return deepcopy(self.places.get(query, []))

    async def value_opportunity(self, payload, displayed_ids, excluded_suggestion_ids=None):
        self.value_calls.append((deepcopy(payload), list(displayed_ids), list(excluded_suggestion_ids or [])))
        result = deepcopy(self.value_boundary)
        result["candidateIds"] = [i for i in result["candidateIds"] if i not in (excluded_suggestion_ids or [])]
        return result

    async def nearby(self, listing_ids, radius, groups, target_id=None):
        self.nearby_calls.append((list(listing_ids), radius, list(groups), target_id))
        return {"summaries": [s for s in (self.summaries or []) if s["listingId"] in listing_ids]}


class FakePlanner:
    def __init__(self, plan):
        self.plan_result = plan
        self.history = None
        self.state = "not-called"
        self.calls = 0

    async def plan(self, question, history, catalog, state, today):
        self.calls += 1
        self.history, self.state, self.today = history, state, today
        result = self.plan_result
        return result(question) if callable(result) else result


class FakeGenerator:
    def __init__(self):
        self.kwargs = None
        self.evidence = None
        self.boundary_decision = {"selectedId": None, "comparedListingIds": [], "advantages": [], "tradeoffs": []}
        self.boundary_calls = []

    async def generate_value_advice(self, question, evidence):
        self.boundary_calls.append(deepcopy(evidence))
        return deepcopy(self.boundary_decision)

    async def generate_answer(self, **kwargs):
        self.kwargs = kwargs
        return "Phòng này có chỗ gửi xe có phí 80.000 đ/tháng."

    async def generate_listing_answer(self, question, evidence):
        self.evidence = deepcopy(evidence)
        answer = await self.generate_answer(question=question,
            context_chunks=[{"content": json.dumps(evidence, ensure_ascii=False)}], audience="USER", mode="listing")
        return ListingGeneratedAnswer(answer, [item["id"] for item in evidence["listings"]])


def settings(**overrides):
    values = dict(nearby_query_enabled=True, nearby_default_radius_m=2000,
                  listing_search_utc_offset=timedelta(hours=7), ai_listing_max_results=20)
    values.update(overrides)
    return SimpleNamespace(**values)


def make_assistant(plan, ids=None, **setting_overrides):
    assistant = ListingAsk.__new__(ListingAsk)
    assistant.settings = settings(**setting_overrides)
    assistant.tools = FakeTools(ids)
    assistant.planner = FakePlanner(plan)
    assistant.generative_client = FakeGenerator()
    assistant.clock = lambda: FIXED_NOW
    assistant.catalog = None
    assistant._base = None
    return assistant


class Conversation:
    """Stores answers like the repository does: the listingContext carries the criteria state."""

    def __init__(self, assistant):
        self.assistant = assistant
        self.messages = []

    async def ask(self, question):
        result = await self.assistant.try_answer(question, self.messages[-8:], "conversation",
                                                 state=self.state())
        self.messages += [{"role": "user", "content": question},
                          {"role": "assistant", "content": result.answer, "status": result.status,
                           "listingContext": result.listing_context}]
        return result

    def state(self):
        from homespace_ai.application.renter_search_state import latest_state
        return latest_state(self.messages)


def leaves(payload):
    return payload["where"]["all"]
