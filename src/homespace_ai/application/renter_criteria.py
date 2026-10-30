"""Deterministic checks on the planner's search update, before it touches state or Core.

The model proposes criteria; these rules enforce what the customer actually said: explicit
categories, occupant counts, budget boundaries, removals, self-descriptions and landmarks.
"""

import re
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import Any

from homespace_ai.application.listing_catalog import CatalogError, SearchCatalog, money
from homespace_ai.application.listing_location import catalog as admin_catalog, key as admin_key, short as admin_short
from homespace_ai.application.renter_dates import resolve as resolve_dates
from homespace_ai.application.renter_search_state import CONTEXT_KEYS, categories as state_categories
from homespace_ai.application.vietnamese_text import fold_text

# Nationwide browse was removed from web/Core; chat search always needs a place.
HYPOTHETICAL = re.compile(r"\b(?:neu|gia su|gia nhu)\b.{0,40}\b(?:thi sao|thi the nao|thi co|the nao)\b")
DROP_WORDS = re.compile(r"\b(?:khong can|khong bat buoc|bo (?:yeu cau|dieu kien|tieu chi|loc)|khong quan trong|sao cung duoc|khong yeu cau)\b")
SELF_DESCRIPTION = re.compile(r"\b(?:minh|toi|em|tui|tu(?:i)? minh|chung toi|chung minh|ban minh)\s+(?:khong|ko|chua bao gio)\s+(?:hut thuoc|nuoi|uong ruou|nhau|nau an)")
RULE_WORDS = re.compile(r"\b(?:cho phep|cam|khong cho|duoc phep|noi quy|chu nha|duoc hut|duoc nuoi)\b")
UNSUPPORTED_WORDS = {
    "QUIETNESS": re.compile(r"\b(?:yen tinh|it on|khong on|on ao)\b"),
    "SECURITY_QUALITY": re.compile(r"\ban ninh (?:tot|cao|on)\b"),
    "TRAVEL_TIME": re.compile(r"\b(?:\d+\s*phut|di bo|thoi gian di|bao lau)\b"),
    "FLOODING": re.compile(r"\b(?:ngap|khong ngap)\b"),
    "LANDLORD_COHABITATION": re.compile(r"\b(?:chung chu|khong chung chu|chu o cung)\b"),
    "NEARBY_SPORTS": re.compile(r"\bgan\s+(?:(?:phong|cho|trung tam|cau lac bo|clb)\s+)?(?:gym|tap gym|the hinh|tap the hinh|phong tap|san the thao|ho boi|be boi)\b"),
}
# Gym/pool as a building amenity ("tòa nhà có gym") is a listing field; near-by sports places are not collected.
IN_BUILDING_SPORTS = re.compile(r"\b(?:co|trong toa(?: nha)?|toa nha co|chung cu co|khu co|du an co)\s+(?:phong\s+)?(?:gym|tap gym|phong tap|ho boi|be boi)\b")
CONTEXT_WORDS = {
    "occupants": re.compile(r"\b(?:nguoi|vo chong|gia dinh|cap doi|o ghep|o mot minh|mot minh|ban be|sinh vien)\b"),
    "motorbikes": re.compile(r"\b(?:xe may|xe|chiec xe)\b"),
    "cars": re.compile(r"\b(?:o to|oto|xe hoi)\b"),
    "electricityKwh": re.compile(r"\b(?:kwh|so dien|dien)\b"),
    "waterM3": re.compile(r"\b(?:m3|khoi|so nuoc|nuoc)\b"),
}
NUM_WORDS = {"mot": 1, "hai": 2, "ba": 3, "bon": 4, "nam": 5, "sau": 6, "bay": 7, "tam": 8, "chin": 9, "muoi": 10}
_N = r"(\d+(?:[.,]\d+)?)"
_U = r"\s*(tr|trieu|cu|m|k|nghin|ngan)\b"
MONEY_FIELDS_BUDGET = ("rent.monthly_amount", "price_amount")
TOTAL_WORDS = re.compile(r"\b(?:tong chi phi|tong tien|tat ca chi phi|bao gom (?:ca )?(?:dien nuoc|phi)|all.?in|chi phi moi thang|tong moi thang)\b")


def boundary_policy(question: str, proposed: Any = None) -> str | None:
    """Persist an explicit no-increase decision; a later 'xem thêm' cannot reset it."""
    text = fold_text(question)
    if HYPOTHETICAL.search(text):
        return None
    if re.search(r"\b(?:khong|dung)\s+(?:(?:the|muon|chap nhan|can)\s+)?(?:tang|vuot|them tien|nang ngan sach|goi y|de xuat)\b|"
                 r"\btuyet doi\s+khong\s+vuot\b|\b(?:het co|het kha nang|khong nhich them|khong du them)\b", text):
        return "DENY"
    if not HYPOTHETICAL.search(text) and re.search(
            r"\b(?:co the|san sang|chap nhan|cho phep)\s+(?:minh\s+|toi\s+)?(?:nhich|tang|nang ngan sach|vuot ngan sach|them tien)\b", text):
        return "ALLOW"
    if proposed == "DENY":
        return "DENY"  # A conservative planner interpretation only suppresses optional advice.
    flexibility = re.search(r"\b(?:nhich|them tien|nang ngan sach|tang ngan sach|noi ngan sach|vuot ngan sach|"
                            r"bo gioi han ngan sach|xem phong dat hon|can nhac phong dat hon)\b", text)
    return proposed if proposed in {"AUTO", "ALLOW"} and flexibility else None


def grounded_needs(question: str, proposed: Any = None) -> list[str]:
    """Qualitative needs stay customer quotes, never invented numeric search thresholds."""
    text = fold_text(question)
    notes = [note.strip()[:220] for note in (proposed if isinstance(proposed, list) else [])
             if isinstance(note, str) and note.strip() and fold_text(note.strip()) in text]
    for clause in re.split(r"[.;\n,]", question):
        if re.search(r"\b(?:uu tien|khong quan trong|khong can|da co (?:san )?(?:giuong|tu|noi that|do)|"
                     r"mang theo|nhieu do|khong gian rong)\b", fold_text(clause)):
            notes.append(clause.strip()[:220])
    return list(dict.fromkeys(notes))[-6:]


def _amount(number: str, unit: str | None) -> Decimal | None:
    return money(number + (unit or ""))


_STEP = {"tr": 1_000_000, "trieu": 1_000_000, "cu": 1_000_000, "m": 1_000_000, "k": 1_000, "nghin": 1_000, "ngan": 1_000}
# "4 triệu rưỡi" and "4tr5" are 4.5 million; "500m"/"30m2" are distances/areas, not money.
_MONEY_TEXT = _N + r"\s*(tr|trieu|cu|m|k|nghin|ngan)(?:\s*(ruoi)\b|(\d{1,3})\b|\b)(?!\s*(?:2|vuong|²))"
PER_PERSON = re.compile(r"\bmoi\s+(?:nguoi|dua|ban|dua mot)\b|/\s*nguoi\b|\bmot nguoi mot\b|\bdau nguoi\b")


def _money_text(number: str, unit: str, half: str | None, tail: str | None) -> Decimal | None:
    base = _amount(number, unit)
    if base is None or (unit == "m" and (tail or base >= 100_000_000)):
        return None
    step = Decimal(_STEP[unit])
    if half:
        base += step / 2
    elif tail and unit != "m":
        base += step * Decimal(tail) / (Decimal(10) ** len(tail))
    return base


FEE_WORDS = re.compile(r"\b(?:dien|nuoc|don gia|(?<!chi )phi|gui xe|de xe|giu xe|coc|dat coc|wifi|internet|mang|rac|dich vu|quan ly|ve sinh)\b")
RENT_WORDS = re.compile(r"\b(?:tien phong|tien thue|gia thue|gia phong|ngan sach|chi phi|tong)\b")


def _fee_amount(text: str, start: int) -> bool:
    """'nước ..., đơn giá tối đa 25 nghìn' bounds a fee, not the rent; the planner keeps it on the fee field."""
    clause = re.split(r"[,;.!?]", text[max(0, start - 40):start])[-1]
    fee = [m.end() for m in FEE_WORDS.finditer(clause)]
    rent = [m.end() for m in RENT_WORDS.finditer(clause)]
    return bool(fee) and (not rent or fee[-1] > rent[-1])


FEE_WORDS = re.compile(r"\b(?:dien|nuoc|don gia|(?<!chi )phi|gui xe|de xe|giu xe|coc|dat coc|wifi|internet|mang|rac|dich vu|quan ly|ve sinh)\b")
RENT_WORDS = re.compile(r"\b(?:tien phong|tien thue|gia thue|gia phong|ngan sach|chi phi|tong)\b")


def _fee_amount(text: str, start: int) -> bool:
    """'nước ..., đơn giá tối đa 25 nghìn' bounds a fee, not the rent; the planner keeps it on the fee field."""
    clause = re.split(r"[,;.!?]", text[max(0, start - 40):start])[-1]
    fee = [m.end() for m in FEE_WORDS.finditer(clause)]
    rent = [m.end() for m in RENT_WORDS.finditer(clause)]
    return bool(fee) and (not rent or fee[-1] > rent[-1])


def budget_bounds(question: str, occupants_count: int | None = None) -> list[tuple[str, Decimal]]:
    """'dưới 5tr' is lt, 'tối đa 5tr' is lte, 'ít nhất 3tr' is gte, 'trên' gt, 'khoảng/tầm' approx.

    A per-person amount ('mỗi người tối đa 1,5 triệu') is the group's rent only when the head count is known;
    otherwise it is left to the planner rather than read as the whole room's budget.
    """
    text = fold_text(question)
    found: list[tuple[str, Decimal]] = []

    def scaled(value: Decimal | None, start: int, end: int) -> Decimal | None:
        if value is None or _fee_amount(text, start):
            return None
        if PER_PERSON.search(text[max(0, start - 30):end + 14]):
            return value * occupants_count if occupants_count else None
        return value

    lone = r"(?:\s*(tr|trieu|cu|m|k|nghin|ngan))?"
    range_pattern = r"\btu\s+" + _N + lone + r"\s*(?:den|toi|-)\s*" + _MONEY_TEXT
    for match in re.finditer(range_pattern, text):
        unit = match[2] or match[4]
        low = scaled(_money_text(match[1], unit, None, None), match.start(), match.end())
        high = scaled(_money_text(match[3], match[4], match[5], match[6]), match.start(), match.end())
        if low is not None and high is not None:
            found += [("gte", low), ("lte", high)]
    text_wo_ranges = re.sub(range_pattern, lambda m: " " * len(m[0]), text)
    words = (r"(duoi hoac bang|khong qua|khong vuot qua|toi da|duoi|tren|hon|it nhat|toi thieu|khoang|tam tam|tam|chung|"
             r"tro lai|xap xi)")
    ops = {"duoi hoac bang": "lte", "khong qua": "lte", "khong vuot qua": "lte", "toi da": "lte", "duoi": "lt",
           "tren": "gt", "hon": "gt", "it nhat": "gte", "toi thieu": "gte", "khoang": "approx", "tam tam": "approx",
           "tam": "approx", "chung": "approx", "xap xi": "approx", "tro lai": "lte"}
    for match in re.finditer(r"\b" + words + r"\s+" + _MONEY_TEXT, text_wo_ranges):
        value = scaled(_money_text(match[2], match[3], match[4], match[5]), match.start(), match.end())
        if value is not None:
            found.append((ops[match[1]], value))
    for match in re.finditer(_MONEY_TEXT + r"\s*(tro xuong|tro lai|tro len|do lai)", text_wo_ranges):
        value = scaled(_money_text(match[1], match[2], match[3], match[4]), match.start(), match.end())
        if value is not None:
            found.append(("gte" if match[5] == "tro len" else "lte", value))
    return found


def occupants(question: str, current: int | None = None) -> int | None:
    """Stated head count; 'thêm 1 người' adds to the current count unless a total ('thành 3 người') is given."""
    text = fold_text(question)
    numbers = r"(\d{1,2}|" + "|".join(NUM_WORDS) + r")"
    total = re.search(r"\b(?:thanh|tong cong|tong|la)\s+" + numbers + r"\s*nguoi\b", text)
    added = re.search(r"\bthem\s+" + numbers + r"\s*nguoi\b", text)
    if total:
        match = total
    elif added and current:
        count = int(added[1]) if added[1].isdigit() else NUM_WORDS[added[1]]
        return current + count if 1 <= current + count <= 20 else None
    else:
        match = re.search(r"\b(?:o|cho|nhom|gom|cung|danh cho|phong)\s+" + numbers + r"\s*(?:nguoi|minh|ban|dua)\b", text)
    if not match:
        match = re.search(r"\b(\d{1,2}|" + "|".join(NUM_WORDS) + r")\s*(?:nguoi o|nguoi\b)", text)
    if not match:
        return None
    value = match[1]
    number = int(value) if value.isdigit() else NUM_WORDS[value]
    return number if 1 <= number <= 20 else None


def vehicles(question: str) -> dict[str, int]:
    text = fold_text(question)
    result = {}
    bike = re.search(r"\b(\d{1,2}|" + "|".join(NUM_WORDS) + r")\s*(?:chiec\s+)?xe(?:\s+may)?\b(?!\s*(?:hoi|o to|oto))", text)
    car = re.search(r"\b(\d{1,2}|" + "|".join(NUM_WORDS) + r")\s*(?:chiec\s+)?(?:o to|oto|xe hoi)\b", text)
    for key, match in (("motorbikes", bike), ("cars", car)):
        if match:
            value = match[1]
            result[key] = int(value) if value.isdigit() else NUM_WORDS[value]
    return result


def mentioned_categories(question: str) -> list[str]:
    text = fold_text(question)
    found = []
    if re.search(r"\bnha nguyen can\b|\bnha rieng\b|\bthue nguyen can\b|\bnha pho\b", text):
        found.append("HOUSE")
    if re.search(r"\bcan ho\b|\bchung cu\b|\bstudio\b|\bcondotel\b", text):
        found.append("APARTMENT")
    if re.search(r"\bphong tro\b|\bnha tro\b|\bday tro\b|\btro\b(?!\s*(?:ly|xuong|len|lai|thanh|ve|giup|nen|choi))", text):
        found.append("ROOM")
    return found


NEAR_WORDS = re.compile(r"\b(?:gan|canh|quanh|sat|doi dien|cach|ke ben|lan can|di bo|di hoc|di lam|hoc o|hoc tai|lam o|lam tai)\b")


@lru_cache(maxsize=1)
def admin_names() -> frozenset[str]:
    cities, wards, aliases = admin_catalog()
    return frozenset({admin_short(name) for _, name in wards.values()} | {admin_short(name) for name in cities.values()} | set(aliases))


def _grounded(place: str, text: str) -> bool:
    words = admin_key(place).split()
    return bool(words) and sum(re.search(r"\b" + re.escape(w) + r"\b", text) is not None for w in words) >= 0.6 * len(words)


def _constraint(raw: dict[str, Any]) -> dict[str, Any]:
    value = raw.get("values") if raw.get("op") == "in" else raw.get("value")
    return {"field": raw.get("field"), "op": raw.get("op"), "value": value,
            "strength": raw.get("strength") if raw.get("strength") in {"hard", "preference"} else "hard"}


def normalize(question: str, args: dict[str, Any], catalog: SearchCatalog, state: dict[str, Any] | None,
              today: date) -> dict[str, Any]:
    """Return a validated state update. Raises CatalogError for criteria Core cannot express."""
    text = fold_text(question)
    constraints = [_constraint(c) for c in args.get("constraints") or [] if isinstance(c, dict)]
    alternatives = [[_constraint(c) for c in (group.get("constraints") or []) if isinstance(c, dict)]
                    for group in args.get("alternatives") or [] if isinstance(group, dict)]
    alternatives = [g for g in alternatives if g]
    remove = [str(v) for v in args.get("removeFields") or [] if isinstance(v, str)]

    # Category: what the customer named, not a persona guess ("sinh viên" is not ROOM).
    named = mentioned_categories(question)
    if len(named) == 1:
        constraints = [c for c in constraints if c["field"] != "category"]
        constraints.append({"field": "category", "op": "eq", "value": named[0], "strength": "hard"})
    elif len(named) > 1:
        constraints = [c for c in constraints if c["field"] != "category"]
        if not alternatives:
            constraints.append({"field": "category", "op": "in", "value": sorted(named), "strength": "hard"})
    else:
        constraints = [c for c in constraints if c["field"] != "category"]
    if len(named) > 1 and alternatives:
        for group in alternatives:
            if not any(c["field"] == "category" for c in group):
                raise CatalogError("INVALID_FILTER", "category", "Each alternative needs its own category")

    # Occupants are context for per-person rent and a capacity criterion (>=, never equality).
    # The model fills unstated slots with defaults (1 người, 0 xe, 0 kWh); keep only what the customer said.
    context = {k: v for k, v in (args.get("context") or {}).items()
               if k in CONTEXT_KEYS and type(v) is int and (v > 0 or k in {"motorbikes", "cars"})
               and CONTEXT_WORDS[k].search(text)}
    count = occupants(question, ((state or {}).get("context") or {}).get("occupants"))
    capacity_cap = re.search(r"\b(?:toi da|suc chua|chi cho)\b.{0,12}\bnguoi\b", text)
    if count is not None:
        context["occupants"] = count
    for group in [constraints, *alternatives]:
        for c in group:
            if c["field"].endswith("max_occupants") and c["op"] == "eq" and not capacity_cap:
                c["op"] = "gte"
    if count is not None and not any(c["field"].endswith("max_occupants") for c in constraints + [x for g in alternatives for x in g]):
        constraints.append({"field": "occupancy.max_occupants", "op": "gte", "value": str(count), "strength": "hard"})
    planned_categories = {str(c["value"]).upper() for c in constraints if c["field"] == "category" and c["op"] == "eq"} or None
    if planned_categories is None and state:
        planned_categories = state_categories(state)
    for c in constraints:
        scope = c["field"].split(".")[0]
        if c["field"].endswith(".max_occupants") and scope in {"room", "house", "apartment"} and planned_categories != {scope.upper()}:
            c["field"] = "occupancy.max_occupants"
    context.update({k: v for k, v in vehicles(question).items() if k not in context})

    # Budget: monthly rent of the customer's group, with boundaries exactly as said.
    bounds = budget_bounds(question, context.get("occupants") or ((state or {}).get("context") or {}).get("occupants"))
    total = bool(TOTAL_WORDS.search(text))
    budget_field = "cost.fixed_monthly_total" if total and "cost.fixed_monthly_total" in catalog.fields else "rent.monthly_amount"
    for c in constraints:
        if c["field"] == "price_amount" and budget_field in catalog.fields:
            c["field"] = budget_field
    if bounds:
        money_constraints = [c for c in constraints if catalog.is_money(c["field"]) and c["field"] in
                             {"rent.monthly_amount", "cost.fixed_monthly_total", "cost.estimated_monthly_total", "price_amount"}]
        for op, amount in bounds:
            if op == "approx":
                near = {amount, amount * Decimal("0.8"), amount * Decimal("1.2")}
                constraints = [c for c in constraints if not (c in money_constraints and money(c["value"]) in near)]
                constraints.append({"field": budget_field, "op": "approx", "value": str(int(amount)), "strength": "preference"})
                continue
            same = [c for c in money_constraints if money(c["value"]) == amount]
            if same:
                for c in same:
                    c["op"] = op
            else:
                constraints.append({"field": budget_field, "op": op, "value": str(int(amount)), "strength": "hard"})

    # "Không cần thang máy" removes the elevator requirement; it is not "must have no elevator".
    if DROP_WORDS.search(text):
        kept = []
        for c in constraints:
            negative = c["op"] == "ne" or str(c["value"]).lower() == "false"
            if negative and (c["field"].startswith(("amenity.", "furnishing.", "room.has_", "house.has_")) or c["field"] in {"media.IMAGE", "media.VIDEO"}):
                remove.append(c["field"])
            else:
                kept.append(c)
        constraints = kept
    # "Mình không hút thuốc" describes the renter, not a house rule they require.
    if SELF_DESCRIPTION.search(text) and not RULE_WORDS.search(text):
        constraints = [c for c in constraints if not c["field"].startswith("policy.")]

    # Balcony/parking: do not narrow a generic need to a guessed type or fee.
    if re.search(r"\bban cong\b", text):
        exact = "PRIVATE" if re.search(r"\bban cong rieng\b", text) else "SHARED" if re.search(r"\bban cong chung\b", text) else None
        absent = bool(re.search(r"\bkhong (?:co )?ban cong\b", text))
        for c in constraints:
            if c["field"] == "room.balcony_type" and exact is None:
                c.update(field="room.has_balcony", op="eq", value="false" if absent else "true")
            elif c["field"] == "room.balcony_type":
                c.update(op="eq", value=exact)
    if re.search(r"\b(?:gui xe|de xe|do xe)\b", text) and not re.search(r"\b(?:mien phi|khong (?:mat|thu|tinh) phi|co phi|thu phi|tra phi|tinh phi)\b", text):
        constraints = [c for c in constraints if not c["field"].startswith("charge.MOTORBIKE_PARKING.")]
        for c in constraints:
            if c["field"] == "room.parking_policy" and c["op"] == "eq" and c["value"] in {"FREE", "PAID"}:
                c.update(op="ne", value="NONE")

    if UNSUPPORTED_WORDS["NEARBY_SPORTS"].search(text) and not IN_BUILDING_SPORTS.search(text):
        constraints = [c for c in constraints if c["field"] not in {"amenity.GYM", "amenity.SWIMMING_POOL"}]

    # Ward/city are resolved once to admin codes; text predicates on them ("Hồ Chí Minh" vs "Thành phố Hồ Chí Minh") miss.
    location = dict(args["location"]) if isinstance(args.get("location"), dict) else {}
    cities, wards, _ = admin_catalog()
    kept = []
    for c in constraints:
        if c["field"] not in {"ward", "ward_id", "city", "city_id"}:
            kept.append(c)
            continue
        slot = "ward" if c["field"].startswith("ward") else "city"
        value = c["value"] if isinstance(c["value"], str) else None
        if value and c["field"] == "ward_id":
            value = wards[value][1] if value in wards and admin_short(wards[value][1]) in text else None
        elif value and c["field"] == "city_id":
            value = cities.get(value)
        if value and not isinstance(location.get(slot), str):
            location[slot] = value
    constraints = kept

    # Stored street names vary in prefix and case ("Đường Lê Lợi", "Lê Lợi").
    for c in constraints:
        if c["field"] == "street" and c["op"] == "eq" and isinstance(c["value"], str):
            name = re.sub(r"^\s*(?:đường|duong|đ\.|d\.)\s*", "", c["value"], flags=re.IGNORECASE).strip()
            c.update(op="contains", value=name or c["value"])

    # Dates use the request clock; a model-invented date never overrides the customer's phrase.
    dates = resolve_dates(question, today)
    if dates.move_in_by is not None:
        constraints = [c for c in constraints if c["field"] != "available_from"]
        constraints.append({"field": "available_from", "op": "lte", "value": dates.move_in_by.isoformat(), "strength": "hard"})
    if dates.lease_months is not None:
        constraints = [c for c in constraints if c["field"] != "minimum_lease_months"]
        constraints.append({"field": "minimum_lease_months", "op": "lte", "value": str(dates.lease_months), "strength": "hard"})

    validated = [catalog.validate(c) for c in constraints]
    validated_alternatives = [[catalog.validate(c) for c in group] for group in alternatives]

    place = args.get("place") if isinstance(args.get("place"), dict) else {}
    place_text = place.get("query") if isinstance(place.get("query"), str) and place["query"].strip() else None
    if place_text and admin_short(place_text) in admin_names():
        # "Hạnh Thông" / "An Nhơn" is an area, not a landmark to measure distance from.
        if not isinstance(location.get("ward"), str) and not isinstance(location.get("city"), str):
            location["ward"] = place_text
        place_text = None
    elif place_text and not NEAR_WORDS.search(text) and not _grounded(place_text, text):
        # A landmark the customer never named (e.g. a school sharing the ward's name) is not a criterion.
        place_text = None
    keyword = args.get("keyword") if isinstance(args.get("keyword"), str) and args["keyword"].strip() else None
    if place_text and keyword:
        # A landmark is proven by coordinates, never by its name appearing in a title.
        a, b = fold_text(keyword).strip(), fold_text(place_text).strip()
        if a in b or b in a:
            keyword = None
    if place_text:
        validated = [c for c in validated if c["field"] not in {"street", "display", "ward", "ward_id"}
                     or fold_text(str(c["value"])) in text]
    nearby = []
    sports_requested = False
    kinds = {k["code"]: k for k in catalog.kinds}
    for item in args.get("nearby") or []:
        if not isinstance(item, dict):
            continue
        radius = item.get("radiusMeters") if type(item.get("radiusMeters")) is int else None
        if item.get("kind"):
            if item["kind"] not in kinds:
                raise CatalogError("INVALID_NEARBY", "nearby.kind", "Unknown nearby kind", sorted(kinds))
            nearby.append({"kind": item["kind"], "group": None, "radiusMeters": radius})
        elif item.get("group") == "SPORTS":
            sports_requested = True
        elif item.get("group"):
            nearby.append({"kind": None, "group": item["group"], "radiusMeters": radius})
    unsupported = [u for u in args.get("unsupported") or [] if isinstance(u, dict) and isinstance(u.get("code"), str)]
    if sports_requested and not any(u["code"] == "NEARBY_SPORTS" for u in unsupported):
        unsupported.append({"code": "NEARBY_SPORTS", "text": question[:200]})
    for code, pattern in UNSUPPORTED_WORDS.items():
        if pattern.search(text) and not any(u["code"] == code for u in unsupported):
            unsupported.append({"code": code, "text": question[:200]})
    sort = args.get("sort") if isinstance(args.get("sort"), str) and args["sort"] in catalog.sorts else None
    if sort == "price_asc":
        # Raw amounts of different price units are not comparable.
        sort = "fixed_cost_asc" if total else "rent_asc"
    return {
        "constraints": validated, "alternatives": validated_alternatives, "removeFields": remove,
        "location": {"ward": location.get("ward"), "city": location.get("city")}
        if isinstance(location.get("ward"), str) or isinstance(location.get("city"), str) else None,
        "nationwide": False,
        "placeQuery": place_text, "placeRadiusMeters": place.get("radiusMeters") if type(place.get("radiusMeters")) is int else None,
        "nearby": nearby, "context": context, "sort": sort, "keyword": keyword,
        "limit": args.get("limit") if type(args.get("limit")) is int and args["limit"] > 0 else None,
        "unsupported": unsupported, "moreResults": args.get("moreResults") is True,
        "boundaryAdvice": boundary_policy(question, args.get("boundaryAdvice")),
        "needs": grounded_needs(question, args.get("needs")),
        "valuePriorities": [item for item in (args.get("valuePriorities") or [])[:6] if isinstance(item, dict)
                            and item.get("field") in catalog.fields and item.get("direction") in {"HIGHER", "LOWER", "IGNORE"}
                            and isinstance(item.get("quote"), str) and item["quote"].strip()
                            and len(item["quote"]) <= 220 and fold_text(item["quote"].strip()) in text],
        "withinDisplayed": args.get("withinDisplayed") is True, "hypothetical": bool(HYPOTHETICAL.search(text)),
        "dateAmbiguous": dates.ambiguous is not None and dates.move_in_by is None,
    }
