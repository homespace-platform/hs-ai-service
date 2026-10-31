"""Build bounded model context from an owned conversation's durable, full-history memory.

The transcript is not rewritten. Only references/criteria are recovered; prices and public
availability must always be read from Core again.
"""
from copy import deepcopy
from dataclasses import dataclass

from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.listing_context import valid_ids
from homespace_ai.application.renter_guidance import PROFILE_TERMS, profile_keyword
from homespace_ai.application.vietnamese_text import fold_text


@dataclass
class ConversationMemory:
    history: list[dict]
    state: dict | None
    repaired: bool = False


def _query_identity(state):
    return rs.fingerprint(state | {"sort": state.get("sort") or "newest"})


def conversation_memory(conversation: dict) -> ConversationMemory:
    messages = conversation.get("messages") or []
    state = rs.sanitize(conversation.get("renterSearchState")) or rs.latest_state(messages)
    history = [{key: message.get(key) for key in ("role", "content", "status", "listingContext", "contractContext", "appointmentContext")}
               for message in messages[-8:]]
    for message in history:
        message["role"] = message["role"] or "user"
        message["content"] = message["content"] or ""
    memory = ConversationMemory(history, state)
    keyword = (state or {}).get("keyword")
    if not keyword or fold_text(keyword).strip() not in set().union(*PROFILE_TERMS.values()):
        return memory

    # Trace the introduction of this keyword, not merely the last message that inherited it.
    # An explicitly requested literal title/content search is never automatically removed.
    previous_keyword, question, origin = None, "", None
    for message in messages:
        if message.get("role") == "user":
            question = message.get("content") or ""
        elif message.get("role") == "assistant":
            saved = (message.get("listingContext") or {}).get("searchState")
            if not isinstance(saved, dict):
                continue
            current_keyword = saved.get("keyword")
            if current_keyword != previous_keyword:
                origin = question if current_keyword == keyword else None
            previous_keyword = current_keyword
    if not origin or not profile_keyword(keyword, origin):
        return memory

    repaired = deepcopy(state)
    repaired["keyword"] = None
    if (repaired.get("pending") or {}).get("type") == "EMPTY_RESULT":
        repaired["pending"] = None
    # Publish the repair via the existing optimistic revision guard on the next answer.
    repaired["revision"] += 1
    restored = []
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        context = message.get("listingContext") or {}
        prior = rs.sanitize(context.get("searchState"))
        ids = valid_ids(context.get("listingIds") or [])
        if prior and ids and message.get("status") in {None, "ANSWERED"} and _query_identity(prior) == _query_identity(repaired):
            restored = ids
            break
    history = deepcopy(history)
    if restored:
        for message in reversed(history):
            if message.get("role") == "assistant" and isinstance(message.get("listingContext"), dict):
                # Correct only the in-memory reference projection, never the saved transcript.
                message["listingContext"] = {**message["listingContext"], "listingIds": restored, "searchState": repaired}
                break
    return ConversationMemory(history, repaired, repaired=True)
