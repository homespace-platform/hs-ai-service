"""Versioned renter search criteria kept across a conversation.

The state stores intent and references only (criteria, confirmed location/target, displayed IDs).
Prices, availability and every listing fact are re-read from Core before each answer.
"""

import hashlib
import json
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from homespace_ai.application.vietnamese_text import VietnamesePattern

STATE_VERSION = 1
CONTEXT_VERSION = 2
MAX_CONSTRAINTS = 40
MAX_ALTERNATIVES = 4
MAX_NEARBY = 3
MAX_SHOWN = 200
STRENGTHS = {"hard", "preference"}
OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "approx"}
CONTEXT_KEYS = ("occupants", "motorbikes", "cars", "electricityKwh", "waterM3")
PENDING_TYPES = {"LOCATION", "TARGET", "CONFLICT", "CATEGORY_CHANGE", "MODEL", "EMPTY_RESULT", "CONTEXT", "DATE", "INTAKE"}
SCOPED_PREFIXES = {"room.": "ROOM", "house.": "HOUSE", "apartment.": "APARTMENT"}

RESET = VietnamesePattern(
    r"(?:tìm|bắt đầu|làm) lại từ đầu|bỏ hết (?:các |mọi )?(?:tiêu chí|điều kiện|bộ lọc|yêu cầu)|"
    r"xóa (?:hết |tất cả )?(?:các )?(?:tiêu chí|bộ lọc|điều kiện)|tìm mới hoàn toàn|\breset\b|"
    r"không giữ (?:các )?(?:tiêu chí|điều kiện|yêu cầu) cũ")


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "revision": 0, "turn": 0, "constraints": [], "alternatives": [],
            "location": None, "nationwide": False, "target": None, "nearby": [], "context": {},
            "sort": None, "keyword": None, "unsupported": [], "pending": None, "needs": [], "valuePriorities": [],
            "renterProfile": {}, "intakeOpen": [], "guidanceAsked": [],
            "boundaryAdvice": "AUTO", "boundaryArea": None, "boundarySeen": {"fingerprint": None, "ids": []},
            "shown": {"fingerprint": None, "ids": []}}


def _uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _constraint(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    field, op, strength = raw.get("field"), raw.get("op"), raw.get("strength", "hard")
    value = raw.get("value")
    if not isinstance(field, str) or not 1 <= len(field) <= 120 or op not in OPERATORS or strength not in STRENGTHS:
        return None
    if isinstance(value, list):
        if not 1 <= len(value) <= 20 or not all(isinstance(v, (str, int, float)) for v in value):
            return None
    elif not _scalar(value) or value is None or (isinstance(value, str) and not 1 <= len(value) <= 160):
        return None
    turn = raw.get("turn")
    label = raw.get("label")
    return {"field": field, "op": op, "value": value, "strength": strength,
            "turn": turn if isinstance(turn, int) and turn >= 0 else 0,
            "label": label[:120] if isinstance(label, str) else None}


def sanitize(raw: Any) -> dict[str, Any] | None:
    """Validate stored/client state; invalid parts are dropped, never trusted for access."""
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return None
    state = empty_state()
    state["revision"] = raw["revision"] if isinstance(raw.get("revision"), int) and raw["revision"] >= 0 else 0
    state["turn"] = raw["turn"] if isinstance(raw.get("turn"), int) and raw["turn"] >= 0 else 0
    state["constraints"] = [c for c in map(_constraint, raw.get("constraints") or []) if c][:MAX_CONSTRAINTS]
    alternatives = []
    for group in (raw.get("alternatives") or [])[:MAX_ALTERNATIVES]:
        items = [c for c in map(_constraint, group if isinstance(group, list) else []) if c]
        if items:
            alternatives.append(items[:10])
    state["alternatives"] = alternatives if len(alternatives) >= 2 else []
    location = raw.get("location")
    if isinstance(location, dict):
        clean = {key: str(location[key])[:160] for key in ("wardId", "ward", "cityId", "city")
                 if isinstance(location.get(key), str) and location[key]}
        state["location"] = clean or None
    state["nationwide"] = raw.get("nationwide") is True
    target = raw.get("target")
    if isinstance(target, dict) and _uuid(target.get("placeId")) and isinstance(target.get("radiusMeters"), int):
        state["target"] = {"placeId": _uuid(target["placeId"]), "name": str(target.get("name") or "")[:200],
                           "address": str(target.get("address") or "")[:300],
                           "radiusMeters": min(max(target["radiusMeters"], 100), 5000)}
    nearby = []
    for item in (raw.get("nearby") or [])[:MAX_NEARBY]:
        if isinstance(item, dict) and isinstance(item.get("radiusMeters"), int) and (
                isinstance(item.get("kind"), str) or isinstance(item.get("group"), str)):
            nearby.append({"kind": item.get("kind") if isinstance(item.get("kind"), str) else None,
                           "group": item.get("group") if isinstance(item.get("group"), str) else None,
                           "radiusMeters": min(max(item["radiusMeters"], 100), 5000)})
    state["nearby"] = nearby
    context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    state["context"] = {key: context[key] for key in CONTEXT_KEYS
                        if type(context.get(key)) is int and 0 <= context[key] <= 100000}
    state["sort"] = raw["sort"] if isinstance(raw.get("sort"), str) and len(raw["sort"]) <= 40 else None
    state["keyword"] = raw["keyword"][:160] if isinstance(raw.get("keyword"), str) and raw["keyword"].strip() else None
    state["unsupported"] = [{"code": str(u.get("code"))[:40], "text": str(u.get("text") or "")[:200]}
                            for u in (raw.get("unsupported") or [])[:10] if isinstance(u, dict) and u.get("code")]
    notes = raw.get("needs") if isinstance(raw.get("needs"), list) else []
    state["needs"] = list(dict.fromkeys(note.strip()[:220] for note in notes if isinstance(note, str) and note.strip()))[-6:]
    profile = raw.get("renterProfile") if isinstance(raw.get("renterProfile"), dict) else {}
    from homespace_ai.application.renter_guidance import PERSONAS
    state["renterProfile"] = {k: v for k, v in profile.items() if (k == "persona" and isinstance(v, str) and v in PERSONAS)
                              or (k in {"studyPlace", "workPlace"} and isinstance(v, str) and 0 < len(v) <= 160)}
    state["intakeOpen"] = [slot for slot in (raw.get("intakeOpen") or []) if isinstance(slot, str) and slot in {"CATEGORY", "BUDGET", "OCCUPANTS"}][:3]
    state["guidanceAsked"] = [text[:250] for text in raw.get("guidanceAsked") or [] if isinstance(text, str)][-6:]
    priorities = raw.get("valuePriorities") if isinstance(raw.get("valuePriorities"), list) else []
    state["valuePriorities"] = [{"field": item["field"], "direction": item["direction"], "quote": item["quote"][:220]}
                                for item in priorities if isinstance(item, dict) and isinstance(item.get("field"), str)
                                and 1 <= len(item["field"]) <= 120 and item.get("direction") in {"HIGHER", "LOWER", "IGNORE"}
                                and isinstance(item.get("quote"), str) and item["quote"].strip()][-6:]
    pending = raw.get("pending")
    if isinstance(pending, dict) and pending.get("type") in PENDING_TYPES:
        state["pending"] = {"type": pending["type"], "question": str(pending.get("question") or "")[:600],
                            "fields": [str(f)[:120] for f in (pending.get("fields") or [])[:10]],
                            "candidates": [{"placeId": _uuid(c.get("placeId")), "name": str(c.get("name") or "")[:200],
                                            "address": str(c.get("address") or "")[:300]}
                                           for c in (pending.get("candidates") or [])[:10]
                                           if isinstance(c, dict) and _uuid(c.get("placeId"))],
                            "radiusMeters": pending.get("radiusMeters") if isinstance(pending.get("radiusMeters"), int) else None,
                            "proposed": sanitize(pending.get("proposed")) if isinstance(pending.get("proposed"), dict) else None}
    shown = raw.get("shown") if isinstance(raw.get("shown"), dict) else {}
    state["shown"] = {"fingerprint": shown.get("fingerprint") if isinstance(shown.get("fingerprint"), str) else None,
                      "ids": [i for i in map(_uuid, shown.get("ids") or []) if i][:MAX_SHOWN]}
    if raw.get("boundaryAdvice") in {"AUTO", "ALLOW", "DENY"}:
        state["boundaryAdvice"] = raw["boundaryAdvice"]
    area = raw.get("boundaryArea")
    state["boundaryArea"] = area if isinstance(area, str) and re_full_area(area) else None
    seen = raw.get("boundarySeen") if isinstance(raw.get("boundarySeen"), dict) else {}
    state["boundarySeen"] = {"fingerprint": seen.get("fingerprint") if isinstance(seen.get("fingerprint"), str) else None,
                             "ids": list(dict.fromkeys(i for i in map(_uuid, seen.get("ids") or []) if i))[-40:]}
    return state


def latest_state(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Newest state over the FULL transcript, not just the prompt window. Legacy v1 contexts have none."""
    for message in reversed(messages or []):
        if message.get("role") != "assistant":
            continue
        context = message.get("listingContext")
        if isinstance(context, dict) and context.get("version") == CONTEXT_VERSION:
            state = sanitize(context.get("searchState"))
            if state is not None:
                return state
    return None


def bound(op: str) -> str:
    return "lower" if op in {"gt", "gte"} else "upper" if op in {"lt", "lte"} else "exact"


def _supersedes(new: dict[str, Any], old: dict[str, Any]) -> bool:
    if new["field"] != old["field"]:
        return False
    if new["op"] == "contains" and old["op"] == "contains":
        return str(new["value"]).casefold() == str(old["value"]).casefold()
    if new["strength"] != old["strength"] and bound(new["op"]) == bound(old["op"]):
        return True
    return bound(new["op"]) == "exact" or bound(old["op"]) == "exact" or bound(new["op"]) == bound(old["op"])


def _removed(code: str, constraint: dict[str, Any]) -> bool:
    return constraint["field"] == code or constraint["field"].startswith(code + ".")


def merge(previous: dict[str, Any] | None, update: dict[str, Any], question: str) -> dict[str, Any]:
    """Apply one planner update to the stored criteria.

    Omitted criteria are kept: removal requires an explicit removeFields entry, so a short answer
    ("Hạnh Thông") or a single change ("tăng ngân sách lên 5 triệu") never loses older needs.
    A reset needs the customer's words, not only a model flag.
    """
    base = deepcopy(previous) if previous else empty_state()
    turn = base["turn"] + 1
    if RESET.search(question):
        keep_shown = base["shown"]
        keep_area = base.get("boundaryArea")
        base = empty_state()
        base["shown"] = keep_shown
        base["boundaryArea"] = keep_area
    base["turn"] = turn
    base["pending"] = None
    if update.get("boundaryAdvice") in {"AUTO", "ALLOW", "DENY"}:
        base["boundaryAdvice"] = update["boundaryAdvice"]
    for code in update.get("removeFields") or []:
        if not isinstance(code, str):
            continue
        if code == "location":
            base["location"], base["nationwide"] = None, False
        elif code == "needs":
            base["needs"] = []
            base["valuePriorities"] = []
        elif code == "valuePriorities":
            base["valuePriorities"] = []
        elif code == "renterProfile":
            base["renterProfile"] = {}
        elif code == "target":
            base["target"] = None
            if base["sort"] == "target_distance":
                base["sort"] = None
        elif code in {"keyword", "sort"}:
            base[code] = None
        elif code == "alternatives":
            base["alternatives"] = []
        elif code.startswith("nearby"):
            wanted = code.partition(":")[2]
            base["nearby"] = [n for n in base["nearby"] if wanted and wanted not in {n.get("kind"), n.get("group")}]
        elif code.startswith("context."):
            base["context"].pop(code.partition(".")[2], None)
        else:
            base["constraints"] = [c for c in base["constraints"] if not _removed(code, c)]
            groups = [[c for c in group if not _removed(code, c)] for group in base["alternatives"]]
            groups = [g for g in groups if g]
            if len(groups) == 1:
                base["constraints"].extend(groups[0])
            base["alternatives"] = groups if len(groups) >= 2 else []
    for raw in update.get("constraints") or []:
        constraint = _constraint({**raw, "turn": turn}) if isinstance(raw, dict) else None
        if constraint is None:
            continue
        base["constraints"] = [c for c in base["constraints"] if not _supersedes(constraint, c)]
        base["constraints"].append(constraint)
    base["constraints"] = base["constraints"][-MAX_CONSTRAINTS:]
    alternatives = [[c for c in (_constraint({**raw, "turn": turn}) for raw in group if isinstance(raw, dict)) if c]
                    for group in (update.get("alternatives") or []) if isinstance(group, list)]
    alternatives = [group[:10] for group in alternatives if group][:MAX_ALTERNATIVES]
    if len(alternatives) >= 2:
        base["alternatives"] = alternatives
        # A field described per alternative must not also be AND-ed at the top level.
        fields = {c["field"] for group in alternatives for c in group}
        base["constraints"] = [c for c in base["constraints"] if c["turn"] == turn or c["field"] not in fields]
    location = update.get("location")
    if isinstance(location, dict) and any(isinstance(location.get(k), str) and location[k] for k in ("wardId", "ward", "cityId", "city")):
        base["location"] = {k: location[k] for k in ("wardId", "ward", "cityId", "city")
                            if isinstance(location.get(k), str) and location[k]}
        base["nationwide"] = False
    if update.get("nationwide") is True:
        base["nationwide"], base["location"] = True, None
    if isinstance(update.get("target"), dict):
        base["target"] = update["target"]
    radius = update.get("targetRadiusMeters")
    if base["target"] and type(radius) is int:
        base["target"]["radiusMeters"] = min(max(radius, 100), 5000)
    for item in update.get("nearby") or []:
        if not isinstance(item, dict) or type(item.get("radiusMeters")) is not int:
            continue
        key = item.get("kind") or item.get("group")
        if not isinstance(key, str):
            continue
        base["nearby"] = [n for n in base["nearby"] if (n.get("kind") or n.get("group")) != key]
        base["nearby"].append({"kind": item.get("kind"), "group": item.get("group") if not item.get("kind") else None,
                               "radiusMeters": min(max(item["radiusMeters"], 100), 5000)})
    base["nearby"] = base["nearby"][-MAX_NEARBY:]
    for key, value in (update.get("context") or {}).items():
        if key in CONTEXT_KEYS and type(value) is int and 0 <= value <= 100000:
            base["context"][key] = value
    if isinstance(update.get("sort"), str) and update["sort"]:
        base["sort"] = update["sort"]
    if isinstance(update.get("keyword"), str) and update["keyword"].strip():
        base["keyword"] = update["keyword"].strip()[:160]
    known = {u["code"] for u in base["unsupported"]}
    for item in update.get("unsupported") or []:
        if isinstance(item, dict) and isinstance(item.get("code"), str) and item["code"] not in known:
            base["unsupported"].append({"code": item["code"][:40], "text": str(item.get("text") or "")[:200]})
            known.add(item["code"])
    base["unsupported"] = base["unsupported"][-10:]
    base["needs"] = list(dict.fromkeys(base.get("needs", []) + [note[:220] for note in update.get("needs") or []
                                                               if isinstance(note, str) and note.strip()]))[-6:]
    priorities = {item["field"]: item for item in base.get("valuePriorities") or []}
    for item in update.get("valuePriorities") or []:
        if isinstance(item, dict) and isinstance(item.get("field"), str):
            priorities[item["field"]] = item
    base["valuePriorities"] = list(priorities.values())[-6:]
    base.setdefault("renterProfile", {}).update(update.get("renterProfile") or {})
    base["intakeOpen"] = list(dict.fromkeys(base.get("intakeOpen", []) + (update.get("intakeOpen") or [])))
    return base


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def conflicts(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Contradictory hard bounds of one field. They are asked about, never swapped or dropped."""
    found = []
    by_field: dict[str, list[dict[str, Any]]] = {}
    for c in state["constraints"]:
        if c["strength"] == "hard":
            by_field.setdefault(c["field"], []).append(c)
    for field, items in by_field.items():
        lowers = [c for c in items if bound(c["op"]) == "lower"]
        uppers = [c for c in items if bound(c["op"]) == "upper"]
        for low in lowers:
            for high in uppers:
                a, b = _number(low["value"]), _number(high["value"])
                if a is None or b is None:
                    a, b = str(low["value"]), str(high["value"])
                if a > b or (a == b and (low["op"] == "gt" or high["op"] == "lt")):
                    found.append({"field": field, "lower": low, "upper": high})
        exact = [c for c in items if c["op"] == "eq"]
        negated = [c for c in items if c["op"] == "ne"]
        for e in exact:
            if any(str(n["value"]).casefold() == str(e["value"]).casefold() for n in negated):
                found.append({"field": field, "lower": e, "upper": next(n for n in negated if str(n["value"]).casefold() == str(e["value"]).casefold())})
    return found


def categories(state: dict[str, Any]) -> set[str] | None:
    """Explicit category scope, or None when every supported housing type is allowed."""
    for c in state["constraints"]:
        if c["field"] == "category" and c["strength"] == "hard":
            if c["op"] == "eq":
                return {str(c["value"]).upper()}
            if c["op"] == "in" and isinstance(c["value"], list):
                return {str(v).upper() for v in c["value"]}
    return None


def incompatible(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Category-scoped criteria that cannot apply to the chosen categories (e.g. room.* for APARTMENT)."""
    allowed = categories(state)
    if allowed is None:
        return []
    result = []
    for c in state["constraints"]:
        scope = next((cat for prefix, cat in SCOPED_PREFIXES.items() if c["field"].startswith(prefix)), None)
        if scope and scope not in allowed:
            result.append(c)
    return result


def without(state: dict[str, Any], removed: list[dict[str, Any]]) -> dict[str, Any]:
    result = deepcopy(state)
    result["constraints"] = [c for c in result["constraints"] if c not in removed]
    return result


def has_scope(state: dict[str, Any]) -> bool:
    """Search needs a place: ward/city, or a chosen landmark. Nationwide browse is not supported."""
    location = state.get("location") or {}
    return bool(location.get("wardId") or location.get("cityId") or state.get("target"))


def fingerprint(state: dict[str, Any]) -> str:
    material = {key: state.get(key) for key in ("location", "nationwide", "target", "nearby", "context", "sort", "keyword")}
    material["constraints"] = sorted(
        [(c["field"], c["op"], json.dumps(c["value"], ensure_ascii=False)) for c in state["constraints"] if c["strength"] == "hard"])
    material["alternatives"] = [sorted((c["field"], c["op"], json.dumps(c["value"], ensure_ascii=False)) for c in g)
                                for g in state["alternatives"]]
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def re_full_area(value: str) -> bool:
    return len(value) == 24 and all(c in "0123456789abcdef" for c in value)


def boundary_area(state: dict[str, Any] | None) -> str | None:
    state = state or {}
    location = state.get("location") or {}
    identity = [location.get("cityId"), location.get("wardId")]
    if not any(identity):
        identity = ["target", (state.get("target") or {}).get("placeId")]
        if not identity[1]:
            return None
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]


def boundary_turn(previous: dict | None, current: dict, *, more: bool, within: bool) -> bool:
    if more or within:
        return False
    before = previous or {}
    last = before.get("boundaryArea")
    if not last and (before.get("shown") or {}).get("ids"):
        last = boundary_area(before)  # Existing conversations must not repeat the same area's advice.
    return boundary_area(current) != last or last is None


def boundary_fingerprint(state: dict[str, Any]) -> str:
    """Advice is also scoped to preferences; its history never resets the main pagination."""
    preferences = sorted((c["field"], c["op"], str(c["value"])) for c in state["constraints"] if c["strength"] == "preference")
    return hashlib.sha256(json.dumps([fingerprint(state), preferences, state.get("needs") or [], state.get("valuePriorities") or [], state.get("renterProfile") or {}], ensure_ascii=False).encode()).hexdigest()[:24]


def boundary_exclusions(state: dict[str, Any]) -> list[str]:
    seen = state.get("boundarySeen") or {}
    return list(seen.get("ids") or []) if seen.get("fingerprint") == boundary_fingerprint(state) else []


def record_boundary(state: dict[str, Any], identifier: str) -> dict[str, Any]:
    result = deepcopy(state)
    result["boundarySeen"] = {"fingerprint": boundary_fingerprint(state),
                              "ids": list(dict.fromkeys(boundary_exclusions(state) + [identifier]))[-40:]}
    return result


def _leaf(c: dict[str, Any]) -> dict[str, Any]:
    value = c["value"]
    if isinstance(value, bool):
        value = str(value).lower()
    return {"field": c["field"], "op": c["op"], "value": value}


def core_request(state: dict[str, Any], *, limit: int, scope_ids: list[str] | None = None,
                 exclude_ids: list[str] | None = None, evidence: bool = True) -> dict[str, Any]:
    """Every HARD criterion is sent to Core (v2). Preferences rank/explain, they never filter."""
    leaves: list[dict[str, Any]] = [_leaf(c) for c in state["constraints"] if c["strength"] == "hard"]
    location = state.get("location") or {}
    if location.get("wardId"):
        leaves.append({"field": "ward_id", "op": "eq", "value": location["wardId"]})
    if location.get("cityId"):
        leaves.append({"field": "city_id", "op": "eq", "value": location["cityId"]})
    alternatives = [[_leaf(c) for c in group if c["strength"] == "hard"] for group in state["alternatives"]]
    alternatives = [group for group in alternatives if group]
    if len(alternatives) >= 2:
        leaves.append({"any": [{"all": group} for group in alternatives]})
    nearby = []
    if state.get("target"):
        nearby.append({"placeId": state["target"]["placeId"], "radiusMeters": state["target"]["radiusMeters"]})
    for item in state.get("nearby") or []:
        nearby.append({"kind": item["kind"], "radiusMeters": item["radiusMeters"]} if item.get("kind")
                      else {"group": item["group"], "radiusMeters": item["radiusMeters"]})
    sort = state.get("sort") or "newest"
    if sort == "target_distance" and not state.get("target"):
        sort = "newest"
    payload: dict[str, Any] = {"version": 2, "filters": [], "where": {"all": leaves}, "sort": sort, "limit": limit,
                               "context": dict(state.get("context") or {}), "evidence": evidence}
    if nearby:
        payload["nearbyAll"] = nearby[:MAX_NEARBY]
    if state.get("keyword"):
        payload["keyword"] = state["keyword"]
    if scope_ids is not None:
        payload["scopeIds"] = scope_ids
    if exclude_ids:
        payload["excludeIds"] = exclude_ids[:50]
    return payload


def record_shown(state: dict[str, Any], ids: list[str], *, more: bool) -> dict[str, Any]:
    """Pagination is bound to the criteria fingerprint; changed criteria restart it."""
    result = deepcopy(state)
    current = fingerprint(result)
    previous = result["shown"]["ids"] if more and result["shown"]["fingerprint"] == current else []
    result["shown"] = {"fingerprint": current, "ids": list(dict.fromkeys(previous + ids))[-MAX_SHOWN:]}
    return result


def listing_context(ids: list[str], state: dict[str, Any] | None) -> dict[str, Any]:
    valid = [i for i in map(_uuid, ids) if i]
    if state is None:
        return {"version": 1, "listingIds": list(dict.fromkeys(valid))}
    return {"version": CONTEXT_VERSION, "listingIds": list(dict.fromkeys(valid)), "searchState": state}
