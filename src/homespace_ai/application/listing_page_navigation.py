"""Bounded listing-page references and deterministic back/forward navigation, never snapshots."""
import re
from copy import deepcopy

from fastapi import HTTPException

from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.listing_context import current_scope, valid_ids
from homespace_ai.application.listing_table import listing_table
from homespace_ai.application.vietnamese_text import fold_text
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.tools.listings import ListingToolError

MAX_PAGES = 50


def intent(question):
    text = fold_text(question).strip(" .,!?:;")
    if re.fullmatch(r"(?:quay lai|tro lai|ve|xem lai) (?:trang|nhom(?: phong)?|danh sach) (?:dau|dau tien|ban dau)|xem lai (?:cac |\d+ )?phong (?:dau tien|ban dau)", text):
        return "FIRST", 1
    if re.fullmatch(r"quay lai|tro lai|lui lai|trang truoc|nhom truoc|(?:quay lai|tro lai|xem lai|cho (?:toi|minh) xem lai) (?:trang|nhom(?: phong)?|danh sach) (?:truoc|vua roi)(?: do)?", text):
        return "BACK", 1
    match = re.fullmatch(r"(?:quay lai|tro lai|lui|lui lai) (\d+) (?:trang|nhom)", text)
    if match and 1 <= int(match[1]) <= MAX_PAGES:
        return "BACK", int(match[1])
    if re.fullmatch(r"xem them|trang (?:sau|tiep|tiep theo)|tiep theo|xem (?:trang|nhom) (?:sau|tiep theo)", text):
        return "NEXT", 1
    return None


def clean(raw, shown):
    empty = {"fingerprint": None, "pages": [], "index": 0}
    if not isinstance(raw, dict) or not isinstance(raw.get("fingerprint"), str) or not isinstance(raw.get("pages"), list):
        return empty
    allowed = set(shown)
    pages = []
    for page in raw["pages"][-MAX_PAGES:]:
        if not isinstance(page, list):
            continue
        ids = [identifier for identifier in valid_ids(page) if identifier in allowed]
        if ids and ids not in pages:
            pages.append(ids)
    index = raw.get("index")
    index = min(max(index, 0), len(pages)-1) if type(index) is int and pages else 0
    return {"fingerprint": raw["fingerprint"], "pages": pages, "index": index}


def record(state, ids, *, more):
    current = rs.fingerprint(state)
    nav = clean(state.get("pageHistory"), state["shown"]["ids"])
    pages = nav["pages"] if more and nav["fingerprint"] == current else []
    page = valid_ids(ids)
    if page and page not in pages:
        pages.append(page)
    return clean({"fingerprint": current, "pages": pages, "index": len(pages)-1}, state["shown"]["ids"])


def recover(state, messages):
    """Project old chat pages from proven cumulative shown IDs, not message prose or guessed chunks."""
    if not state:
        return state
    fingerprint = rs.fingerprint(state)
    existing = clean(state.get("pageHistory"), state["shown"]["ids"])
    if existing["pages"] and existing["fingerprint"] == fingerprint:
        return state
    if state["shown"]["fingerprint"] != fingerprint:
        return state  # Criteria changed; older search pages cannot silently replace them.
    allowed = set(state["shown"]["ids"])
    pages, seen, last_scope = [], set(), []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        context = message.get("listingContext") or {}
        if not isinstance(context, dict):
            continue
        prior = rs.sanitize(context.get("searchState"))
        if not prior or rs.fingerprint(prior) != fingerprint:
            continue
        shown = set((prior.get("shown") or {}).get("ids") or [])
        if not shown or not shown <= allowed:
            continue
        if not seen <= shown:
            pages, seen = [], set()  # Same criteria were explicitly searched again from the start.
        ids = valid_ids(context.get("listingIds") or [])
        if ids:
            last_scope = ids
        if ids and set(ids) <= shown and not set(ids) <= seen:
            pages.append(ids)
        seen = shown
    if not pages:
        return state
    index = next((i for i in range(len(pages)-1, -1, -1) if last_scope and set(last_scope) <= set(pages[i])), len(pages)-1)
    result = deepcopy(state)
    result["pageHistory"] = clean({"fingerprint": fingerprint, "pages": pages, "index": index}, state["shown"]["ids"])
    return result


async def answer(assistant, question, history, request_id, state, action):
    nav = clean((state or {}).get("pageHistory"), (state or {}).get("shown", {}).get("ids", []))
    valid = state and nav["fingerprint"] == rs.fingerprint(state) and nav["pages"]
    if not valid:
        if action[0] == "NEXT":
            return None
        return AskResult(answer="Mình chưa có nhóm phòng trước đó để quay lại theo các tiêu chí hiện tại. Bạn muốn tìm lại nhóm phòng theo nhu cầu nào?",
            status="ANSWERED", citations=[], request_id=request_id, listing_context=assistant.context(current_scope(history), state))
    index = nav["index"]
    target = 0 if action[0] == "FIRST" else max(0, index-action[1]) if action[0] == "BACK" else index+1
    if target >= len(nav["pages"]):
        return None  # Discover a NEW page through the normal bounded search, excluding all seen IDs.
    if target == index and action[0] == "BACK":
        return AskResult(answer="Bạn đang ở nhóm phòng đầu tiên. Bạn có thể nói “xem thêm” để chuyển sang nhóm tiếp theo.",
            status="ANSWERED", citations=[], request_id=request_id,
            listing_context=assistant.context(nav["pages"][index], state), suggested_questions=["Xem thêm"])
    changed = deepcopy(state)
    changed["pageHistory"] = {**nav, "index": target}
    changed["pending"] = None
    ids = nav["pages"][target]
    try:
        result = await assistant.tools.search_v2(rs.core_request(changed, limit=len(ids), scope_ids=ids, evidence=False))
        allowed = set(result["listingIds"])
        listings, _ = await assistant._details([identifier for identifier in ids if identifier in allowed], request_id)
    except (ListingToolError, HTTPException, ValueError, TypeError, KeyError) as error:
        assistant._log_failure(request_id, "page_navigation", error)
        return assistant._unavailable(request_id, current_scope(history), state)
    live = [item["id"] for item in listings]
    if not live:
        return AskResult(answer="Các phòng trong nhóm này hiện không còn hiển thị hoặc không còn khớp tiêu chí. Bạn có thể quay lại nhóm khác hoặc xem nhóm tiếp theo.",
            status="ANSWERED", citations=[], request_id=request_id, listing_context=assistant.context([], changed),
            suggested_questions=["Quay lại", "Xem thêm"])
    text = f"Mình hiển thị lại **{len(live)} phòng ở nhóm {target+1}** theo các tiêu chí đang giữ."
    if len(live) < len(ids):
        text += " Một số tin trong nhóm cũ không còn hiển thị hoặc không còn khớp điều kiện nên mình không đưa vào."
    response = AskResult(answer=text + "\n\n" + listing_table(listings, live, []), status="ANSWERED", citations=[],
        request_id=request_id, listing_context=assistant.context(live, changed), listing_ids=live,
        suggested_questions=(["Quay lại"] if target else []) + ["Xem thêm", "Các phòng trên có những tiện ích nào xung quanh?"])
    return await assistant._advise_main(question, request_id, response, listings, changed)
