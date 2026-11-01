"""Per-turn presentation intent. Stores references/topic codes, never a second cache of business facts."""
from dataclasses import dataclass, field
from homespace_ai.application.vietnamese_text import fold_text


@dataclass
class AnswerContext:
    domain: str
    intent: str
    topics: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)

    def metadata(self):
        return {"version": 1, "domain": self.domain, "intent": self.intent,
                "topics": list(dict.fromkeys(self.topics))[:8], "scope": self.scope[:20]}


def listing_focus(question, sections=None):
    from homespace_ai.application.listing_table import requested_sections
    from homespace_ai.application.amenity_intent import NEARBY_INTENT
    text = fold_text(question)
    if NEARBY_INTENT.search(question):
        return "NEARBY"
    if any(term in text for term in ("vi sao", "tai sao")):
        return "EXPLAIN_CHOICE"
    sections = sections if sections is not None else requested_sections(question)
    if "charges" in sections or any(term in text for term in ("tiet kiem", "re hon", "re nhat", "tien thue thap")):
        return "COST"
    if any(term in text for term in ("dien tich", "rong hon", "rong nhat", "khong gian")):
        return "SPACE"
    if any(s in sections for s in ("amenities", "furnishings")):
        return "AMENITIES"
    return "OVERALL"


def scoped_facts(facts, focus):
    def allowed(path):
        if focus == "NEARBY":
            return path.startswith("nearby.")
        if focus == "COST":
            return path.startswith(("charges.", "cost.")) or path == "pricing.amount"
        if focus == "SPACE":
            return path == "areaM2" or path.endswith((".bedroomCount", ".bathroomCount", ".maxOccupants", ".hasMezzanine"))
        if focus == "AMENITIES":
            return path.startswith(("amenities.", "furnishings.")) or path.endswith((".kitchenType", ".restroomType", ".balconyType", ".hasWindow", ".furnishingStatus", ".hasMezzanine"))
        return True
    return {path: fact for path, fact in facts.items() if allowed(path)}
