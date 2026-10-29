"""Load the renter acceptance corpus and evaluate what is checkable without a model.

The offline part only runs the deterministic text guards (budget words, head count, category words,
dates, nationwide, unsupported needs). It never proves model NLU; live results stay NOT_RUN until the
opt-in runner records them.
"""
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from homespace_ai.application.listing_catalog import SearchCatalog
from homespace_ai.application.listing_limits import search_limit
from homespace_ai.application.listing_location import LocationClarification, resolve_location_filters
from homespace_ai.application.renter_criteria import normalize
from homespace_ai.application.vietnamese_text import fold_text

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "renter_corpus_v1.json"
CATALOG_PATH = ROOT.parent / "fixtures" / "core_catalog_v2.json"
SOURCE = ROOT.parents[2] / "hs-infra" / "documents" / "ask" / "AI_RENTER_MANUAL_ACCEPTANCE_QUESTIONS.md"
PSEUDO_FIELDS = {"nationwide", "target", "target.radiusMeters", "nearby", "nearby.kind", "nearby.group", "sort", "limit",
                 "keyword", "scan", "appointment"}
STATUSES = {"PASS", "FAIL", "MISSING_FIXTURE", "NOT_RUN"}
EMPTY_PLAN = {"constraints": [], "alternatives": [], "location": {"ward": None, "city": None}, "nationwide": False,
              "place": {"query": None, "radiusMeters": None}, "nearby": [], "context": {}, "sort": None, "limit": None,
              "keyword": None, "removeFields": [], "moreResults": False, "withinDisplayed": False, "unsupported": []}


def load() -> dict[str, Any]:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def catalog() -> SearchCatalog:
    return SearchCatalog(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))


def source_inputs() -> dict[str, str]:
    """Customer text per ID from the manual acceptance document (the corpus must not rewrite it)."""
    text = SOURCE.read_text(encoding="utf-8")
    found = {m[1]: m[2].strip() for m in re.finditer(r"^\| (Q\d{3}) \| (.+?) \| .+\|$", text, re.MULTILINE)}
    found.update({m[1]: m[2].strip() for m in re.finditer(r"^\d+\. \*\*(C\d{2}\.\d):\*\* (.+)$", text, re.MULTILINE)})
    return found


def _norm(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(str(v) for v in value)
    if isinstance(value, bool):
        return str(value).lower()
    return None if value is None else str(value)


def observed(question: str, cat: SearchCatalog, today: date, plan_args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Criteria after the deterministic layer; plan_args defaults to an empty model plan (guard-only run)."""
    update = normalize(question, json.loads(json.dumps(plan_args or EMPTY_PLAN)), cat, None, today)
    hard = [(c["field"], c["op"], c["value"]) for c in update["constraints"] if c["strength"] == "hard"]
    hard += [("context." + k, "eq", v) for k, v in update["context"].items()]
    if update["nationwide"]:
        hard.append(("nationwide", "eq", True))
    if update["placeQuery"]:
        hard.append(("target", "eq", update["placeQuery"]))
        if update["placeRadiusMeters"]:
            hard.append(("target.radiusMeters", "eq", update["placeRadiusMeters"]))
    hard += [("nearby.kind" if n["kind"] else "nearby.group", "eq", n["kind"] or n["group"]) for n in update["nearby"]]
    if update["sort"]:
        hard.append(("sort", "eq", update["sort"]))
    hard.append(("limit", "eq", search_limit(question)))
    if update["location"]:
        filters = [{"field": k, "op": "eq", "value": v} for k, v in update["location"].items() if v]
        try:
            hard += [(f["field"], f["op"], f["value"]) for f in resolve_location_filters(
                filters, question, [], {"ward_id": "TEXT", "city_id": "TEXT"})]
        except LocationClarification:
            hard.append(("location", "clarify", None))
    return {"hard": hard, "preferences": [(c["field"], c["op"], c["value"]) for c in update["constraints"] if c["strength"] == "preference"],
            "unsupported": [u["code"] for u in update["unsupported"]]}


def _matches(spec: list[Any], item: tuple[str, str, Any]) -> bool:
    field, op, value = (list(spec) + [None, None])[:3]
    if field == "target" and item[0] == "target" and value is not None:
        return fold_text(str(value)) in fold_text(str(item[2]))
    return item[0] == field and (op is None or item[1] == op) and (value is None or _norm(item[2]) == _norm(value))


def check_guard(case: dict[str, Any], cat: SearchCatalog, today: date) -> list[str]:
    """Return problems; empty means the deterministic layer honours this case's guard."""
    return compare(case.get("guard") or {}, observed(case["input"], cat, today))


def compare(guard: dict[str, Any], seen: dict[str, Any]) -> list[str]:
    problems = []
    for spec in guard.get("required") or []:
        if not any(_matches(spec, item) for item in seen["hard"]):
            problems.append(f"missing {spec}; got {seen['hard']}")
    for spec in guard.get("preferences") or []:
        if not any(_matches(spec, item) for item in seen["preferences"]):
            problems.append(f"missing preference {spec}; got {seen['preferences']}")
    for spec in guard.get("forbidden") or []:
        spec = [spec] if isinstance(spec, str) else spec
        if any(_matches(spec, item) for item in seen["hard"]):
            problems.append(f"forbidden {spec} produced")
    for code in guard.get("unsupported") or []:
        if code not in seen["unsupported"]:
            problems.append(f"unsupported {code} not recorded")
    return problems


def field_codes(case: dict[str, Any]) -> set[str]:
    expect = case.get("expect") or {}
    specs = list(expect.get("required") or []) + list((expect.get("state") or {}).get("required") or [])
    specs += [s for s in expect.get("forbidden") or []]
    specs += [[s] for s in expect.get("preferences") or []] + [[s] for s in expect.get("removed") or []]
    guard = case.get("guard") or {}
    specs += list(guard.get("required") or []) + list(guard.get("preferences") or []) + list(guard.get("forbidden") or [])
    return {(s if isinstance(s, str) else s[0]) for s in specs}
