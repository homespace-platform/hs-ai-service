"""Contextual boundary advice: model judges value, application verifies every displayed fact.

The hard-budget results are never edited. Core supplies a bounded candidate pool using the only
allowed relaxation: at most 500k above the customer's budget ceiling, not a displayed room's rent.
"""
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from homespace_ai.application.vietnamese_text import VietnamesePattern
from homespace_ai.tools.listings import ListingToolError

VALUE_REFERENCE = VietnamesePattern(r"(?:căn|phòng|nhà|tin|chỗ)\s+(?:được |vừa )?(?:gợi ý thêm|gợi ý|đề xuất thêm|đề xuất|tăng thêm|nâng cấp)")
DECLINED = VietnamesePattern(r"không (?:cần |muốn )?(?:gợi ý|đề xuất|tăng|thêm tiền|nâng ngân sách)|không thể (?:tăng|vượt)|tuyệt đối không (?:tăng|vượt)|chỉ (?:xem|lấy|hiện|đúng)\s*\d+\s*(?:tin|phòng|căn)")
CHEAPEST_ONLY = VietnamesePattern(r"(?:chỉ|duy nhất).{0,25}(?:rẻ nhất|gần nhất)|không (?:được |thể )?vượt ngân sách")
_LABELS = {"areaM2": "Diện tích", "availableFrom": "Ngày có thể nhận", "maxMotorbikeCount": "Số xe máy tối đa",
           "maxCarCount": "Số ô tô tối đa", "bedroomCount": "Phòng ngủ", "bathroomCount": "Phòng tắm/WC",
           "maxOccupants": "Số người ở tối đa", "restroomType": "Nhà vệ sinh", "kitchenType": "Bếp",
           "balconyType": "Ban công", "hasWindow": "Cửa sổ", "hasMezzanine": "Gác lửng",
           "furnishingStatus": "Nội thất", "floorNumber": "Tầng", "accessHoursType": "Giờ giấc ra vào",
           "accessType": "Lối đi", "electricMeterType": "Đồng hồ điện", "waterMeterType": "Đồng hồ nước",
           "parkingPolicy": "Chính sách gửi xe", "totalFloors": "Số tầng", "hasRooftop": "Sân thượng",
           "hasGarage": "Gara", "minimumLeaseMonths": "Thời hạn thuê tối thiểu", "depositMonths": "Số tháng cọc",
           "depositAmount": "Tiền cọc cố định", "paymentCycle": "Chu kỳ thanh toán", "includedInRent": "Đã gồm trong tiền thuê"}
_ENUMS = {"PRIVATE": "Riêng", "SHARED": "Chung", "NONE": "Không có", "UNFURNISHED": "Không nội thất",
          "BASIC": "Cơ bản", "PARTIALLY_FURNISHED": "Một phần", "FULLY_FURNISHED": "Đầy đủ", "LUXURY": "Cao cấp",
          "FLEXIBLE": "Tự do", "CURFEW": "Có giờ giới hạn", "SEPARATE": "Riêng biệt", "COMMON": "Dùng chung",
          "INDIVIDUAL": "Riêng", "PAID": "Có thu phí", "FREE": "Không thu phí riêng", "NOT_AVAILABLE": "Không có",
          "ALLOWED": "Cho phép", "NOT_ALLOWED": "Không cho phép", "APPROVAL_REQUIRED": "Cần thỏa thuận",
          "MONTHLY": "Hằng tháng", "EVERY_2_MONTHS": "2 tháng/lần", "QUARTERLY": "3 tháng/lần",
          "EVERY_6_MONTHS": "6 tháng/lần", "NEGOTIABLE": "Thỏa thuận"}
_CHARGE_LABELS = {"ELECTRICITY": "Tiền điện", "WATER": "Tiền nước", "MANAGEMENT": "Phí quản lý", "INTERNET": "Internet",
                  "SERVICE_OR_GARBAGE": "Phí dịch vụ/rác", "MOTORBIKE_PARKING": "Phí gửi xe máy", "CAR_PARKING": "Phí gửi ô tô",
                  "OVERTIME_AIR_CONDITIONING": "Phí máy lạnh ngoài giờ"}
_BILLING_UNITS = {"PER_KWH": "đ/kWh", "PER_M3": "đ/m³", "PER_PERSON_MONTH": "đ/người/tháng", "PER_MONTH": "đ/tháng",
                  "PER_M2_MONTH": "đ/m²/tháng", "PER_VEHICLE_MONTH": "đ/xe/tháng", "PER_HOUR": "đ/giờ"}
_HIGH = {"areaM2", "bedroomCount", "bathroomCount", "maxOccupants", "maxMotorbikeCount", "maxCarCount"}
_RANKS = {"restroomType": {"SHARED": 0, "PRIVATE": 1}, "kitchenType": {"NONE": 0, "SHARED": 1, "PRIVATE": 2},
          "balconyType": {"NONE": 0, "SHARED": 1, "PRIVATE": 2},
          "furnishingStatus": {"UNFURNISHED": 0, "BASIC": 1, "PARTIALLY_FURNISHED": 1, "FULLY_FURNISHED": 2, "LUXURY": 3}}


def number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ListingToolError("Invalid boundary number")
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise ListingToolError("Invalid boundary number") from None


def eligible(question: str, criteria: dict, payload: dict, listings: list[dict], *, more: bool, within: bool) -> bool:
    # Pagination exclusions concern main results, not the separate boundary pool.
    if within or payload.get("scopeIds") is not None or not 1 <= len(listings) <= 4:
        return False
    if criteria.get("boundaryAdvice") == "DENY" or DECLINED.search(question) or CHEAPEST_ONLY.search(question):
        return False
    if not any(c.get("strength") == "hard" and c.get("field") in {"rent.monthly_amount", "price_amount"}
               and c.get("op") in {"lt", "lte"} for c in criteria.get("constraints") or []):
        return False
    for item in listings:
        pricing = item.get("pricing") or {}
        try:
            if (pricing.get("unit") not in {"MONTH", "ROOM_MONTH"} or pricing.get("currency") != "VND"
                    or number(pricing.get("amount")) <= 0):
                return False
        except ListingToolError:
            return False
    return True


def formatted(value: Any) -> str:
    result = number(value)
    return f"{result:,.0f}".replace(",", ".") if result == result.to_integral() else format(result.normalize(), "f").replace(".", ",")


def _snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _clean(value: str) -> str:
    return re.sub(r"[\[\]_*`<>\\\r\n]", " ", value)[:120].strip()


def _scalar(value: Any) -> bool:
    return value is not None and isinstance(value, (str, int, float, bool)) and not (isinstance(value, str) and len(value) > 100)


def _facts(item: dict, catalog=None, today: date | None = None) -> dict[str, dict]:
    """Dynamic public structured facts, catalog labels, no marketing or invented absence.

    Directions verify obvious factual comparisons, not quality weights or thresholds. The model
    decides whether a difference is material for the customer's context and worth the overrun.
    """
    result: dict[str, dict] = {}

    def add(path, value, code, leaf, unit=None, label=None):
        if not _scalar(value):
            return
        spec = (catalog.fields.get(code) or {}) if catalog else {}
        if isinstance(value, str) and spec.get("kind") in {"INTEGER", "NUMBER"}:
            try:
                numeric = number(value)
                value = int(numeric) if numeric == numeric.to_integral() else float(numeric)
            except ListingToolError:
                return
        if isinstance(value, str) and spec.get("kind") == "TEXT" and leaf != "availableFrom":
            return
        choices = {str(v.get("code")): str(v.get("label")) for v in spec.get("values") or [] if isinstance(v, dict)}
        shown = choices.get(str(value), _ENUMS.get(str(value))) if isinstance(value, str) else None
        if isinstance(value, str) and shown is None and leaf != "availableFrom":
            return
        direction = "HIGHER" if leaf in _HIGH else "LOWER" if leaf == "availableFrom" and today else "CONTEXT"
        if isinstance(value, bool):
            direction = "HIGHER" if (leaf.startswith("has") or path.startswith("amenities.")
                                     or leaf in {"includedInRent", "managementFeeIncluded", "vatIncluded"}) else "CONTEXT"
        if leaf in _RANKS:
            direction = "ORDERED"
        result[path] = {"value": value, "display": shown or value,
                        "label": _clean(label or spec.get("label") or _LABELS.get(leaf, leaf)),
                        "unit": unit or spec.get("unit"), "direction": direction, "leaf": leaf, "code": code}
        if leaf == "availableFrom" and today:
            try:
                result[path]["effectiveDate"] = max(date.fromisoformat(str(value)), today).isoformat()
            except ValueError:
                result.pop(path)

    for key in ("areaM2", "availableFrom", "maxMotorbikeCount", "maxCarCount"):
        add(key, item.get(key), _snake(key), key, "m²" if key == "areaM2" else None)
    for detail, prefix in (("roomDetail", "room"), ("houseDetail", "house"), ("apartmentDetail", "apartment")):
        for key, raw in (item.get(detail) or {}).items():
            add(detail + "." + key, raw, prefix + "." + _snake(key), key)
    pricing = item.get("pricing") or {}
    for key in ("minimumLeaseMonths", "depositMonths", "depositAmount", "paymentCycle", "negotiable", "managementFeeIncluded", "vatIncluded"):
        add("pricing." + key, pricing.get(key), _snake(key), key)
    for row in (item.get("charges") or [])[:24]:
        kind = row.get("chargeType") if isinstance(row, dict) else None
        if kind not in _CHARGE_LABELS:
            continue  # Multiple OTHER rows have no stable identity; never compare unrelated custom fees.
        label, method = _CHARGE_LABELS[kind], row.get("billingMethod")
        prefix = "charges." + kind
        add(prefix + ".includedInRent", row.get("includedInRent"), "charge." + kind + ".included_in_rent", "includedInRent",
            label=label + " đã gồm trong tiền thuê")
        if method in _BILLING_UNITS and row.get("includedInRent") is False and row.get("amount") is not None:
            add(prefix + ".amount", row["amount"], "charge." + kind + ".amount", "amount", _BILLING_UNITS[method], label)
            if prefix + ".amount" in result:
                result[prefix + ".amount"]["direction"] = "LOWER"
                result[prefix + ".amount"]["comparisonKey"] = [method, row.get("unit"), False]
    for row in (item.get("amenities") or [])[:24]:
        if isinstance(row, dict) and re.fullmatch(r"[A-Z0-9_]{1,60}", str(row.get("code") or "")):
            code = row["code"]
            add("amenities." + code, True, "amenity." + code, code, label=row.get("name"))
    for row in (item.get("furnishings") or [])[:24]:
        code = row.get("itemCode") if isinstance(row, dict) else None
        if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,60}", code) and isinstance(row.get("quantity"), (int, float)):
            path = "furnishings." + code + ".quantity"
            total = row["quantity"] + (result.get(path) or {}).get("value", 0)
            add(path, total, "furnishing." + code + ".total_quantity", "quantity", "cái", row.get("assetName"))
            result[path]["direction"] = "HIGHER"
    for row in (item.get("policies") or [])[:24]:
        if not isinstance(row, dict):
            continue
        code = row.get("code") or row.get("policyCode")
        if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,60}", code):
            raw = row.get("value") if "value" in row else row.get("policyValue")
            add("policies." + code, raw, "policy." + code + ".value", code)
    return dict(list(result.items())[:80])


def _comparison(candidate: dict, main: dict) -> int | None:
    a, b = candidate["value"], main["value"]
    if candidate.get("unit") != main.get("unit") or candidate.get("comparisonKey") != main.get("comparisonKey"):
        return None
    if a == b:
        return 0
    leaf = candidate["leaf"]
    try:
        if candidate["direction"] == "ORDERED":
            ranks = _RANKS[leaf]
            return (ranks[a] > ranks[b]) - (ranks[a] < ranks[b])
        if leaf == "availableFrom":
            if candidate.get("effectiveDate") is None or main.get("effectiveDate") is None:
                return 2
            x, y = date.fromisoformat(candidate["effectiveDate"]), date.fromisoformat(main["effectiveDate"])
            return (y > x) - (y < x)
        if candidate["direction"] in {"HIGHER", "LOWER"}:
            x, y = (int(a), int(b)) if isinstance(a, bool) and isinstance(b, bool) else (number(a), number(b))
            result = (x > y) - (x < y)
            return result if candidate["direction"] == "HIGHER" else -result
    except (KeyError, ValueError, ListingToolError):
        return None
    return 2  # Context-dependent difference; render only verified values, not an invented quality claim.


def _proofs(raw, chosen: dict, mains: dict, *, advantage: bool) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > 3:
        raise ListingToolError("Invalid boundary proofs")
    result, seen = [], set()
    for proof in raw:
        if not isinstance(proof, dict) or not isinstance(proof.get("field"), str) or proof["field"] in seen:
            raise ListingToolError("Invalid boundary proof")
        path, ids = proof.get("field"), proof.get("againstIds")
        if path not in chosen or not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or any(i not in mains for i in ids):
            raise ListingToolError("Unknown boundary fact or comparison")
        changes = []
        for identifier in ids:
            original = mains[identifier].get(path)
            if original is None:
                continue  # Unknown cannot prove either an advantage or a disadvantage.
            change = _comparison(chosen[path], original)
            if change is None or change == 0 or (change != 2 and (change <= 0 if advantage else change >= 0)):
                continue  # Keep only comparisons proven by Core, never the model's assertion.
            changes.append({"listingId": identifier, "fact": original})
        if changes:
            result.append({"field": path, "fact": chosen[path], "comparisons": changes})
        seen.add(path)
    return result


def _redundant(advantages: list[dict], facts: dict) -> bool:
    """Do not charge more for the same benefits a cheaper main already supplies."""
    for proof in advantages:
        previous = facts.get(proof["field"])
        if previous is None:
            return False
        change = _comparison(proof["fact"], previous)
        if change is None or change > 0:
            return False
    return True


def _requested_context(fact: dict, criteria: dict) -> bool:
    """A different floor/access rule/policy is not an upgrade without a stated related need."""
    wanted = list(criteria.get("constraints") or [])
    for group in criteria.get("alternatives") or []:
        if isinstance(group, list):
            wanted.extend(group)
    code = fact["code"]
    aliases = {code}
    if code.startswith(("room.", "house.", "apartment.")):
        aliases.add("detail." + code.partition(".")[2])
    wanted.extend(item for item in criteria.get("valuePriorities") or [] if item.get("direction") != "IGNORE")
    return any(isinstance(item, dict) and item.get("field") in aliases for item in wanted)


def _ignored(fact: dict, criteria: dict) -> bool:
    fields = {p["field"] for p in criteria.get("valuePriorities") or [] if p.get("direction") == "IGNORE"}
    return fact["code"] in fields or (fact["code"].startswith("furnishing.")
                                      and any(field.endswith(".furnishing_status") for field in fields))


def _context_facts(item: dict, criteria: dict, catalog, today) -> dict:
    facts = _facts(item, catalog, today)
    directions = {p["field"]: p["direction"] for p in criteria.get("valuePriorities") or []
                  if p.get("direction") in {"HIGHER", "LOWER"}}
    for fact in facts.values():
        if fact["code"] in directions and isinstance(fact["value"], (int, float, bool)):
            fact["direction"] = directions[fact["code"]]
    return facts


def _prioritized_pool(pool: dict, criteria: dict) -> dict:
    """Read explicit customer priorities first, without dropping candidates or scoring quality."""
    rows = list(pool.values())
    for preference in reversed(criteria.get("valuePriorities") or []):
        if preference.get("direction") not in {"HIGHER", "LOWER"}:
            continue
        values = {}
        units = set()
        for item in rows:
            fact = next((f for f in item["facts"].values() if f["code"] == preference["field"]), None)
            if fact is not None and isinstance(fact["value"], (int, float, bool)):
                values[item["id"]] = number(int(fact["value"]) if isinstance(fact["value"], bool) else fact["value"])
                units.add(str((fact.get("unit"), fact.get("comparisonKey"))))
        if values and len(units) == 1:
            sign = -1 if preference["direction"] == "HIGHER" else 1
            rows.sort(key=lambda item: (item["id"] not in values, sign * values.get(item["id"], Decimal(0))))
    return {item["id"]: item for item in rows}


def _judge_matrix(rows: list[dict]) -> dict:
    """One field per row, one listing per column; all values and fee units stay intact.

    This removes repeated field names/metadata from up to 12 detailed listings. Only the
    human-readable representation goes to the judge; exact typed facts stay local for validation.
    """
    result = {}
    for facts in rows:
        for path, fact in facts.items():
            result.setdefault(path, {"label": fact["label"], "direction": fact["direction"]})
    for path, row in result.items():
        row["values"] = [_display(facts[path]) if path in facts else None for facts in rows]
        if any(path in facts and facts[path]["label"] != row["label"] for facts in rows):
            row["labels"] = [facts[path]["label"] if path in facts else None for facts in rows]
    return result


def _verified_comparisons(facts: dict, mains: dict) -> dict:
    """Factual deltas, not quality scores. Ordinals refer to mainListings order, starting at1."""
    differences = {}
    for path, fact in facts.items():
        groups = {}
        for ordinal, previous in enumerate(mains.values(), 1):
            if path not in previous:
                continue
            change = _comparison(fact, previous[path])
            if change is None or change == 0:
                continue
            key = "contextDiffersFrom" if change == 2 else "higherBenefitThan" if change > 0 else "tradeoffVersus"
            groups.setdefault(key, []).append(ordinal)
        if groups:
            differences[path] = groups
    return differences


async def evaluate(question: str, criteria: dict, main_listings: list[dict], candidates: list[dict], boundary: dict,
                   generator, catalog=None) -> dict | None:
    if (not main_listings or not candidates or criteria.get("boundaryAdvice") == "DENY"
            or any(item.get("status") != "PUBLISHED" for item in main_listings)):
        return None
    ceiling, extra = number(boundary.get("budgetCeiling")), number(boundary.get("maxExtraMonthly"))
    if ceiling <= 0 or not 0 < extra <= 500000:
        raise ListingToolError("Invalid budget boundary")
    allow_at_ceiling = any(c.get("strength") == "hard" and c.get("field") in {"rent.monthly_amount", "price_amount"}
                           and c.get("op") == "lt" and number(c.get("value")) == ceiling
                           for c in criteria.get("constraints") or [])
    main_ids = [str(UUID(item["id"])) for item in main_listings]
    try:
        request_date = criteria.get("requestDate") or criteria.get("requestToday")
        today = date.fromisoformat(request_date) if request_date else None
    except ValueError:
        today = None
    mains = {item["id"]: _context_facts(item, criteria, catalog, today) for item in main_listings}
    pool = {}
    allowed = set(boundary.get("candidateIds") or [])
    for item in candidates[:8]:
        identifier = str(UUID(item["id"]))
        pricing = item.get("pricing") or {}
        if (identifier not in allowed or identifier in mains or item.get("status") != "PUBLISHED"
                or pricing.get("unit") not in {"MONTH", "ROOM_MONTH"} or pricing.get("currency") != "VND"):
            continue
        rent = number(pricing.get("amount"))
        if (rent > ceiling or allow_at_ceiling and rent == ceiling) and rent <= ceiling + extra:
            pool[identifier] = {"id": identifier, "rent": rent, "facts": _context_facts(item, criteria, catalog, today)}
    if not pool:
        return None
    pool = _prioritized_pool(pool, criteria)
    budget_constraints = [c for c in criteria.get("constraints") or [] if c.get("strength") == "hard"
                          and c.get("field") in {"rent.monthly_amount", "price_amount"} and c.get("op") in {"lt", "lte"}]
    retained_criteria = {k: criteria.get(k) for k in ("constraints", "alternatives", "location", "target", "nearby", "context", "unsupported", "needs", "valuePriorities")}
    retained_criteria["constraints"] = [c for c in criteria.get("constraints") or [] if c not in budget_constraints]
    evidence = {"budgetCeiling": ceiling, "maxExtraMonthly": extra, "budgetField": boundary.get("budgetField"), "today": today,
                "candidateBudgetLimit": ceiling + extra, "mainBudgetConstraints": budget_constraints,
                "boundaryPolicy": criteria.get("boundaryAdvice", "AUTO"), "confirmedCriteria": retained_criteria,
                "mainListings": [{"id": item["id"], "rent": item.get("pricing", {}).get("amount"),
                                  "factColumn": column} for column, item in enumerate(main_listings)],
                "candidates": [{"id": item["id"], "rent": item["rent"], "budgetOverrun": item["rent"] - ceiling,
                                "factColumn": len(main_listings) + column,
                                "verifiedComparisons": _verified_comparisons(item["facts"], mains)} for column, item in enumerate(pool.values())],
                "factMatrix": _judge_matrix(list(mains.values()) + [item["facts"] for item in pool.values()]),
                "readAt": boundary.get("readAt")}
    if len(json.dumps(evidence, ensure_ascii=False, default=str).encode()) > 120000:
        raise ListingToolError("Boundary evidence exceeds its input budget")
    decision = await generator.generate_value_advice(question=question, evidence=evidence)
    if not isinstance(decision, dict):
        raise ListingToolError("Invalid boundary decision")
    identifier = decision.get("selectedId")
    if identifier is None:
        return None
    compared = decision.get("comparedListingIds")
    if identifier not in pool or not isinstance(compared, list) or len(compared) != len(main_ids) or set(compared) != set(main_ids):
        raise ListingToolError("Boundary decision did not compare the complete current page")
    candidate = pool[identifier]
    advantages = _proofs(decision.get("advantages"), candidate["facts"], mains, advantage=True)
    advantages = [proof for proof in advantages if not _ignored(proof["fact"], criteria)]
    if not advantages:
        return None
    if any(proof["fact"]["direction"] == "CONTEXT" and not _requested_context(proof["fact"], criteria)
           for proof in advantages):
        return None
    covered = {c["listingId"] for proof in advantages for c in proof["comparisons"]}
    if covered != set(main_ids):
        return None
    if any(_redundant(advantages, mains[item["id"]]) for item in main_listings
           if number(item.get("pricing", {}).get("amount")) < candidate["rent"]):
        return None
    tradeoffs = _proofs(decision.get("tradeoffs"), candidate["facts"], mains, advantage=False)
    noted = {p["field"] for p in tradeoffs}
    for path, fact in candidate["facts"].items():
        if path in noted or fact["direction"] == "CONTEXT":
            continue
        worse = [i for i in main_ids if path in mains[i] and (_comparison(fact, mains[i][path]) or 0) < 0]
        if worse:
            tradeoffs.append({"field": path, "fact": fact, "comparisons": [{"listingId": i, "fact": mains[i][path]} for i in worse]})
    # The judge chooses whether the complete offer is worthwhile. Known regressions are
    # still disclosed even when it omits them; their count is not a quality score or veto.
    return {"listingId": identifier, "candidateRent": candidate["rent"], "budgetCeiling": ceiling,
            "budgetOverrun": candidate["rent"] - ceiling, "allowAtCeiling": allow_at_ceiling, "comparedListingIds": main_ids,
            "advantages": advantages, "tradeoffs": tradeoffs}


def _display(fact: dict) -> str:
    value = fact["display"]
    if isinstance(value, bool):
        text = "Có" if value else "Không"
    elif isinstance(value, (int, float, Decimal)):
        text = formatted(value)
    else:
        text = _clean(str(value))
    if fact.get("unit") and fact["unit"] not in {"BOOLEAN", "ENUM"}:
        text += " " + _clean(str(fact["unit"]))
    return text


def _statement(proof: dict, ids: list[str]) -> str:
    seen = [f"tin {ids.index(row['listingId']) + 1}: {_display(row['fact'])}" for row in proof["comparisons"]]
    return f"**{proof['fact']['label']}: {_display(proof['fact'])}** (" + "; ".join(seen) + ")"


def render(decision: dict, main_listings: list[dict], boundary: dict) -> str:
    ids = [item["id"] for item in main_listings]
    identifier = str(UUID(decision["listingId"]))
    if identifier in ids or identifier not in boundary.get("candidateIds", []) or set(decision["comparedListingIds"]) != set(ids):
        raise ListingToolError("Invalid rendered boundary reference")
    ceiling, rent, extra = number(boundary["budgetCeiling"]), number(decision["candidateRent"]), number(boundary["maxExtraMonthly"])
    if (not ceiling <= rent <= ceiling + extra or rent == ceiling and not decision.get("allowAtCeiling")
            or number(decision["budgetOverrun"]) != rent - ceiling):
        raise ListingToolError("Rendered boundary exceeds the customer's budget window")
    boundary_text = (f"**vượt ngân sách {formatted(rent-ceiling)}đ/tháng** so với mức {formatted(ceiling)}đ"
                     if rent > ceiling else f"**chạm mức ngân sách {formatted(ceiling)}đ/tháng**")
    gains = "; ".join(_statement(p, ids) for p in decision["advantages"])
    result = (f"**Một lựa chọn ngoài ngân sách để cân nhắc:** [Xem tin gợi ý](/rent/{identifier}) giá "
              f"**{formatted(rent)}đ/tháng**, {boundary_text}. Sau khi đối chiếu cả nhóm, điểm đáng cân nhắc là: {gains}.")
    if decision["tradeoffs"]:
        result += " **Điểm cần cân nhắc:** " + "; ".join(_statement(p, ids) for p in decision["tradeoffs"]) + "."
    return result + " Đây là phương án tùy chọn; ngân sách và danh sách chính vẫn giữ nguyên. Bạn nên xem thêm phí dịch vụ và điều kiện thuê trước khi quyết định."
