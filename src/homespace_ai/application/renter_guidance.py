"""Conversational intake and question suggestions. No search or provider calls live here."""
import re

from homespace_ai.application.vietnamese_text import fold_text

PERSONAS = {"STUDENT", "WORKING", "FAMILY", "REMOTE", "OTHER"}
PROFILE_TERMS = {"FAMILY": {"gia dinh", "ho gia dinh"}, "STUDENT": {"sinh vien", "hoc sinh", "sv"},
                 "WORKING": {"nguoi di lam", "nhan vien", "cong nhan", "dan van phong"},
                 "REMOTE": {"lam viec tu xa", "remote", "work from home"}}
MAIN_REFERENCE = re.compile(r"ban (?:uu tien|khuyen|chon)|phong (?:duoc chon|ban chon|ban uu tien)")


def help_answer(question: str) -> tuple[str, str] | None:
    """The app's own intake questions must help the customer, not repeat an unanswerable slot."""
    text = fold_text(question).strip(" .?!")
    answers = {
        "toi chua biet nen chon khu vuc nao, ban tu van giup duoc khong": ("LOCATION",
            "Mình sẽ giúp bạn khoanh vùng theo nơi bạn thường xuyên cần đến. Bạn đang học hoặc làm ở đâu, hay muốn thuận tiện đến địa điểm nào?"),
        "toi can du tru nhung khoan nao ngoai tien thue": ("BUDGET",
            "Ngoài tiền thuê, bạn nên xem các khoản điện, nước, Internet, dịch vụ/rác và gửi xe được tin đăng công bố; tiền cọc là khoản ban đầu riêng. Bạn muốn giới hạn bao nhiêu mỗi tháng, và mức đó tính riêng tiền thuê hay gồm các khoản phí?"),
        "phong tro, can ho va nha nguyen can phu hop voi nhu cau nao": ("CATEGORY",
            "Phòng trọ thường phù hợp khi cần một không gian ở gọn; căn hộ phù hợp khi muốn bố trí nhiều khu sinh hoạt; nhà nguyên căn phù hợp khi cần sử dụng cả căn nhà. Bạn muốn không gian theo kiểu nào, hay muốn xem nhiều loại rồi so sánh?"),
        "toi nen chon dien tich phong the nao cho so nguoi o": ("OCCUPANTS",
            "Mình sẽ đối chiếu số người ở tối đa được tin đăng cho phép và không gian bạn cần cho ngủ, học hoặc làm việc. Bạn dự định ở mấy người, và có cần tách chỗ ngủ/làm việc không?"),
    }
    return answers.get(text)


def explicit_location(question: str, history: list[dict]) -> dict | None:
    """Keep explicit administrative names even when the model only asks a follow-up."""
    from homespace_ai.application.listing_location import catalog, key, mentioned, resolve_location_filters, LocationClarification
    fields = {"city_id": "TEXT", "ward_id": "TEXT"}
    resolved = resolve_location_filters([], question, history, fields)
    cities, wards, _ = catalog()
    city_id = next((item["value"] for item in resolved if item["field"] == "city_id"), None)
    names = {name for parent, name in wards.values() if (not city_id or parent == city_id) and mentioned(key(name), key(question))}
    if len(names) > 1:
        raise LocationClarification("Bạn muốn tìm tại phường/xã nào trước? Mình sẽ giữ các nhu cầu còn lại.")
    if names:
        resolved = resolve_location_filters([{"field": "ward", "op": "eq", "value": next(iter(names))}], question, history, fields)
    location = {}
    for item in resolved:
        if item["field"] == "city_id":
            location.update(cityId=item["value"], city=cities[item["value"]])
        elif item["field"] == "ward_id":
            location.update(wardId=item["value"], ward=wards[item["value"]][1])
    return location or None


def profile_update(question: str, proposed=None) -> dict:
    text = fold_text(question)
    result = {}
    if re.search(r"\bneu\b.*\bthi\b", text):
        return result
    patterns = (("FAMILY", r"(?:gia dinh (?:toi|minh|em)|(?:toi|minh|em).{0,25}cho gia dinh|vo chong (?:toi|minh|em))"),
                ("REMOTE", r"(?:toi|minh|em).{0,20}(?:lam (?:viec )?(?:tu xa|remote)|work from home)"),
                ("STUDENT", r"\b(?:toi|minh|em|bon minh|tui)\s+(?:(?:hien tai|hien|van|dang)\s+)?(?:la\s+)?(?:sinh vien|hoc sinh|sv)\b"),
                ("WORKING", r"\b(?:toi|minh|em|bon minh|tui)\s+(?:(?:hien tai|hien|da|dang|la)\s+){0,3}(?:nguoi di lam|di lam|dan van phong|nhan vien|cong nhan)\b"))
    for persona, pattern in patterns:
        match = re.search(pattern, text)
        if match and not re.search(r"\b(?:khong|ko|chua)\b", match[0]):
            result["persona"] = persona
            break
    if not result:
        short_answer = text.strip(" .,!?:;")
        for persona, terms in PROFILE_TERMS.items():
            if short_answer in terms:
                result["persona"] = persona
                break
    if isinstance(proposed, dict):
        quote, persona = proposed.get("quote"), proposed.get("persona")
        if (not result.get("persona") and persona in PERSONAS and isinstance(quote, str) and 0 < len(quote) <= 220
                and fold_text(quote) in text and re.search(r"\b(?:toi|minh|em|bon minh|tui)\s+(?:la|dang lam|lam viec|di lam)\b", fold_text(quote))
                and not re.search(r"\b(?:khong|ko|chua|neu|gia su)\b", fold_text(quote))):
            result["persona"] = persona
        for field in ("studyPlace", "workPlace"):
            value = proposed.get(field)
            if isinstance(value, str) and value.strip() and len(value) <= 160 and fold_text(value.strip()) in text:
                result[field] = value.strip()
    return result


def profile_only_reply(question: str) -> bool:
    """Recognize a standalone answer to the renter-purpose question.

    Only a complete, conservative grammar bypasses planning. Any remaining requirement,
    number, place, preference, pagination or literal-search request still goes to the planner.
    A persona alone must never let a model invent new mandatory listing conditions.
    """
    if not profile_update(question).get("persona"):
        return False
    text = fold_text(question).strip(" .,!?:;")
    text = re.sub(r"^(?:da|vang|a)\s*[,.:]?\s*", "", text)
    text = re.sub(r"\s+(?:nhe|nha|a|thoi)(?:\s+(?:nhe|nha|a|thoi))*$", "", text)
    terms = "|".join(re.escape(term) for term in sorted(set().union(*PROFILE_TERMS.values()), key=len, reverse=True))
    subject = r"(?:toi|minh|em|tui|bon minh|chung toi)"
    # An object such as "căn hộ" may be a category change, so it cannot be skipped.
    rental = r"(?:tim|thue|tim thue)"
    text = re.sub(rf"^{subject}\s+(?:(?:muon|can|dang)\s+)?{rental}\s*[,;]\s*", "", text)
    return bool(re.fullmatch(
        rf"(?:(?:{subject})\s+)?(?:(?:hien tai|hien|van|dang|da|se|du dinh|muon|can)\s+)*"
        rf"(?:(?:la|{rental})\s+)?(?:cho\s+)?(?:{terms})(?:\s+(?:toi|minh|em))?", text))


def profile_keyword(keyword: str | None, question: str) -> bool:
    """A renter's self-description is not a title/content predicate.

    Preserve an explicitly requested literal text search, even if the same utterance also
    describes the renter. This also lets old conversation state be repaired with provenance.
    """
    if not isinstance(keyword, str):
        return False
    text = fold_text(question)
    term = fold_text(keyword).strip()
    persona = profile_update(question).get("persona")
    if term not in PROFILE_TERMS.get(persona, set()):
        return False
    marker = r"\b(?:tieu de|mo ta|noi dung|tu khoa|cum tu|co chu|chua chu)\b"
    literal = re.search(marker + r".{0,70}" + re.escape(term) + "|" + re.escape(term) + r".{0,70}" + marker, text)
    return literal is None


def explicit_open_slots(question: str) -> list[str]:
    text = fold_text(question)
    patterns = {"CATEGORY": r"loai (?:nha |hinh )?nao cung duoc|chua (?:chot|quyet dinh) loai|khong gioi han loai",
                "BUDGET": r"chua (?:chot|gioi han|xac dinh) ngan sach|khong gioi han (?:ngan sach|gia)|chi (?:muon )?(?:khao sat|tham khao) gia",
                "OCCUPANTS": r"chua (?:chot|biet|xac dinh) so nguoi|chua biet (?:o )?may nguoi"}
    return [slot for slot, pattern in patterns.items() if re.search(pattern, text)]


def missing_basics(state: dict) -> list[str]:
    from homespace_ai.application.renter_search_state import has_scope, categories, empty_state
    state = empty_state() | state
    opened = set(state.get("intakeOpen") or [])
    constraints = state.get("constraints") or []
    missing = []
    if not has_scope(state):
        missing.append("LOCATION")
    if "BUDGET" not in opened and not any(c["field"] in {"rent.monthly_amount", "price_amount", "cost.fixed_monthly_total", "cost.estimated_monthly_total"}
                                           and c.get("strength") == "hard" for c in constraints):
        missing.append("BUDGET")
    if "CATEGORY" not in opened and not categories(state):
        missing.append("CATEGORY")
    if "OCCUPANTS" not in opened and not (state.get("context") or {}).get("occupants"):
        missing.append("OCCUPANTS")
    return missing


def intake_question(missing: list[str]) -> str:
    questions = {"LOCATION": "Bạn muốn thuê ở khu vực nào, hoặc gần trường/nơi làm việc nào?",
                 "BUDGET": "Ngân sách tối đa mỗi tháng của bạn là bao nhiêu: riêng tiền thuê hay gồm cả các khoản phí?",
                 "CATEGORY": "Bạn muốn tìm phòng trọ, căn hộ hay nhà nguyên căn? Nếu chưa chốt loại hình, bạn cứ nói nhé.",
                 "OCCUPANTS": "Bạn dự định ở mấy người để mình chọn phòng đủ sức chứa?"}
    return " ".join(questions[slot] for slot in missing[:2])


def followup_question(state: dict) -> str | None:
    profile = state.get("renterProfile") or {}
    persona = profile.get("persona")
    questions = []
    if not persona:
        questions.append("Bạn tìm chỗ ở để đi học, đi làm hay cho gia đình? Mình sẽ tư vấn sát nhu cầu của bạn hơn.")
    if persona == "STUDENT" and not profile.get("studyPlace") and not state.get("target"):
        questions.append("Bạn đang học trường nào, và có muốn ưu tiên ở gần trường không?")
    if persona == "WORKING" and not profile.get("workPlace") and not state.get("target"):
        questions.append("Bạn có muốn ưu tiên gần nơi làm việc không? Nếu có, bạn cho mình biết khu vực hoặc nơi làm nhé.")
    if not state.get("needs") and not state.get("valuePriorities"):
        questions.append("Trong các lựa chọn này, bạn ưu tiên tiết kiệm chi phí, không gian rộng hay tiện nghi sinh hoạt hơn?")
    return next((q for q in questions if q not in state.get("guidanceAsked", [])), None)


def suggested_questions(state: dict | None, *, has_results=False, has_boundary=False, has_main=False, no_results=False) -> list[str]:
    state = state or {}
    missing = missing_basics(state)
    if missing and not has_results:
        result = []
        if "LOCATION" in missing:
            result.append("Tôi chưa biết nên chọn khu vực nào, bạn tư vấn giúp được không?")
        if "BUDGET" in missing:
            result.append("Tôi cần dự trù những khoản nào ngoài tiền thuê?")
        if "CATEGORY" in missing:
            result.append("Phòng trọ, căn hộ và nhà nguyên căn phù hợp với nhu cầu nào?")
        if "OCCUPANTS" in missing:
            result.append("Tôi nên chọn diện tích phòng thế nào cho số người ở?")
        return result[:4]
    if no_results:
        return ["Tôi nên cân nhắc điều chỉnh tiêu chí nào để có thêm lựa chọn?", "Có lựa chọn nào ở khu vực lân cận không?"]
    if not has_results:
        return []
    result = ["Vì sao bạn ưu tiên phòng được chọn?"] if has_main else ["Trong các phòng này, tôi nên cân nhắc phòng nào?"]
    result.append("Các phòng trên có những tiện ích nào xung quanh?")
    result.append("Trong các phòng này, các khoản phí hằng tháng khác nhau thế nào?")
    result.append("Cho tôi xem thêm các phòng phù hợp với tiêu chí hiện tại được không?")
    return result[:4]
