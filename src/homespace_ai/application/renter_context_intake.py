"""Ground semantic renter circumstances without generating any listing predicate."""
import asyncio

from homespace_ai.application import renter_guidance as guidance
from homespace_ai.application.vietnamese_text import fold_text
from homespace_ai.core.business_properties import value_advice_timeout_seconds
from homespace_ai.tools.listings import ListingToolError


async def read_context(generator, question: str, current_profile: dict) -> tuple[dict, bool]:
    async with asyncio.timeout(value_advice_timeout_seconds()):
        raw = await generator.generate_renter_context(question=question, current_profile=current_profile)
    if (not isinstance(raw, dict) or not (raw.get("persona") is None or isinstance(raw.get("persona"), str))
            or raw.get("persona") not in guidance.PERSONAS | {None}):
        raise ListingToolError("Invalid renter context")
    groups = raw.get("adviceGroups")
    if (not isinstance(groups, list) or len(groups) > 4
            or any(not isinstance(group, str) or group not in guidance.ADVICE_GROUPS for group in groups)):
        raise ListingToolError("Invalid renter advice groups")
    text = fold_text(question)
    def quotes(key, limit):
        values = raw.get(key)
        if (not isinstance(values, list) or len(values) > limit or any(not isinstance(value, str)
                or not 0 < len(value.strip()) <= 220 or fold_text(value.strip()) not in text for value in values)):
            raise ListingToolError("Ungrounded renter context quotes")
        return list(dict.fromkeys(value.strip() for value in values))

    changed = quotes("newCriteriaQuotes", 8)
    needs = quotes("needs", 6)
    if raw["persona"] is None:
        return {}, bool(changed)
    description = raw.get("descriptionQuote")
    if not isinstance(description, str) or not description.strip() or fold_text(description.strip()) not in text:
        raise ListingToolError("Ungrounded renter description")
    profile = guidance.profile_update(question, {**raw, "quote": description})
    if not profile.get("description"):
        raise ListingToolError("Invalid actual renter description")
    return {"renterProfile": profile, "needs": needs}, bool(changed)
