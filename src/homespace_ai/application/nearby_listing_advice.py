"""Cached POI evidence for an existing shortlist; never contacts a map provider.

Hard criteria (stored and newly stated) are re-checked by Core inside the displayed scope
before presentation, so every displayed room still qualifies. Advice selects one room,
independently of how many columns the customer is comparing.
"""
import re
from typing import Any
from fastapi import HTTPException
from homespace_ai.application.listing_context import context_for, current_scope
from homespace_ai.application.vietnamese_text import fold_text
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.application.listing_table import listing_table
from homespace_ai.application.amenity_intent import NEARBY_INTENT, BUILDING_INTENT
from homespace_ai.application.nearby_table import GROUP_LABELS, render as nearby_table, facts as nearby_facts
from homespace_ai.application.listing_catalog import CatalogError, SearchCatalog
from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.renter_criteria import normalize
from homespace_ai.clients.generative import GenerationUnavailableError
from homespace_ai.tools.listings import ListingToolError
from homespace_ai.core.business_properties import nearby_default_radius_m

def radius_for(question: str, history: list[dict[str, Any]], default: int | None = None) -> int:
    for value in [question, *(m.get("content", "") for m in reversed(history) if m.get("role") == "user")]:
        match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(km|kilomet|m|met)\b", fold_text(value))
        if match and (match[2] in {"km", "kilomet"} or re.search(r"ban kinh|pham vi|trong|quanh|khoang cach", fold_text(value)[:match.start()][-30:])):
            radius = round(float(match[1].replace(",", ".")) * (1000 if match[2] in {"km", "kilomet"} else 1))
            if not 100 <= radius <= 5000:
                raise ValueError("Mình hiện hỗ trợ bán kính từ 100 m đến 5 km.")
            return radius
    return nearby_default_radius_m() if default is None else default


def _context(assistant, ids, state):
    return assistant.context(ids, state) if hasattr(assistant, "context") else context_for(ids)


def _has_hard_criteria(state: dict[str, Any] | None) -> bool:
    return bool(state and (any(c["strength"] == "hard" for c in state["constraints"]) or state["alternatives"]
                           or state.get("target") or state.get("nearby") or (state.get("location") or {}).get("wardId")
                           or (state.get("location") or {}).get("cityId")))


async def _eligible(assistant, question: str, ids: list[str], state: dict[str, Any] | None):
    """Merge the criteria stated in this message and let Core re-check them inside the scope."""
    if not hasattr(assistant, "today"):
        return ids, state, state
    catalog = getattr(assistant, "catalog", None)
    criteria, persist = state, state
    try:
        if catalog is None:
            catalog = SearchCatalog(await assistant.tools.catalog())
            assistant.catalog = catalog
        update = normalize(question, {"constraints": []}, catalog, state, assistant.today())
    except CatalogError:
        return ids, state, state
    changes = {"constraints": update["constraints"], "removeFields": update["removeFields"], "context": update["context"]}
    if update["constraints"] or update["removeFields"] or update["context"]:
        criteria = rs.merge(state, changes, question)
        persist = state if update["hypothetical"] else criteria
    if not _has_hard_criteria(criteria):
        return ids, criteria, persist
    limit = min(len(ids), assistant.tools.max_results)
    result = await assistant.tools.search_v2(rs.core_request(criteria, limit=limit, scope_ids=ids[:limit], evidence=False))
    found = set(result["listingIds"])
    return [value for value in ids if value in found], criteria, persist


async def answer_nearby(assistant, question, history, request_id, arguments, state=None):
    scope = current_scope(history)
    ids = arguments.get("listingIds") or scope
    if not ids or any(value not in scope for value in ids):
        return AskResult(answer="Bạn hãy tìm và chọn nhóm phòng trước để mình đối chiếu tiện ích xung quanh.",
                         status="ANSWERED", citations=[], request_id=request_id, listing_context=_context(assistant, scope, state))
    if len(ids) > 20:
        return AskResult(answer="Bạn chọn tối đa 20 phòng trong một lượt để mình đối chiếu tiện ích nhé.",
                         status="ANSWERED", citations=[], request_id=request_id, listing_context=_context(assistant, scope, state))
    persist = state
    try:
        radius = radius_for(question, [], arguments.get("radiusMeters"))
        eligible, criteria, persist = await _eligible(assistant, question, list(ids), state)
        if not eligible:
            lines = []
            if criteria:
                from homespace_ai.application.use_cases.listing_ask import criteria_lines
                lines = criteria_lines(criteria, getattr(assistant, "catalog", None))
            text = f"Trong {len(ids)} phòng đang xem, không phòng nào còn khớp đủ các điều kiện hiện tại"
            text += (":\n" + "\n".join("- " + line for line in lines)) if lines else "."
            text += "\nMình không xếp hạng phòng không đạt. Bạn muốn tìm nhóm phòng mới theo các điều kiện này, hay đổi điều kiện nào?"
            return AskResult(answer=text, status="NO_RESULTS", citations=[], request_id=request_id,
                             listing_context=_context(assistant, scope, persist), data_sources=["CORE_NEARBY"])
        target_id = arguments.get("targetPlaceId") or ((state or {}).get("target") or {}).get("placeId")
        if arguments.get("placeQuery"):
            resolved = await assistant.tools.resolve_nearby_search({"placeQuery": arguments["placeQuery"], "radiusMeters": radius})
            if resolved.get("needsClarification"):
                names = [str(p.get("name", "")) for p in resolved.get("candidates", [])[:5]]
                text = "Bạn muốn đối chiếu với trường hoặc cơ sở nào cụ thể?"
                if names:
                    text += " Mình thấy các địa điểm: " + "; ".join(names) + "."
                else:
                    text += " Mình chưa tìm thấy địa điểm đó trong kho dữ liệu hiện tại."
                return AskResult(answer=text, status="ANSWERED", citations=[], request_id=request_id,
                                 listing_context=_context(assistant, scope, persist), data_sources=["CORE_NEARBY"])
            target_id = resolved["arguments"]["nearby"]["placeId"]
        # The combined ten-category query covers seven groups; gym/sports are not collected.
        groups = arguments.get("groups") or [code for code in GROUP_LABELS if code != "SPORTS"]
        evidence = await assistant.tools.nearby(eligible, radius, groups, target_id)
        summaries = evidence["summaries"]
        if any(not s.get("enabled") for s in summaries):
            return AskResult(answer="Dữ liệu tiện ích xung quanh hiện chưa được bật. Mình vẫn có thể tư vấn theo giá, diện tích và tiện nghi trong tin đăng.",
                             status="ANSWERED", citations=[], request_id=request_id, listing_context=_context(assistant, scope, persist))
        result_context = _context(assistant, scope, persist) | {"nearby": {"radiusMeters": radius, "groups": groups, "targetPlaceId": target_id}}
        if not summaries:
            return AskResult(answer="Mình chưa đọc được dữ liệu tiện ích của nhóm phòng đang chọn. Bạn có thể thử lại hoặc so sánh các thông tin trong tin đăng.",
                             status="NO_RESULTS", citations=[], request_id=request_id, listing_context=_context(assistant, scope, persist))
        # Preserve all current columns and their ordinals; no arbitrary top-three ranking.
        by_id = {s["listingId"]: s for s in summaries}
        chosen, listings = [], []
        for identifier in eligible:
            if identifier not in by_id:
                continue
            try:
                listings.append(await assistant.tools.detail(identifier))
                chosen.append(by_id[identifier])
            except ListingToolError as error:
                if error.status_code in {404, 410} or str(error) == "Listing is no longer public":
                    continue
                raise
        if not chosen:
            return AskResult(answer="Các phòng phù hợp vừa đổi trạng thái và không còn hiển thị công khai. Bạn muốn tìm lại theo các điều kiện đã lưu không?",
                             status="NO_RESULTS", citations=[], request_id=request_id, listing_context=_context(assistant, [], persist))
        selected = [s["listingId"] for s in chosen]
        text = nearby_table(chosen, listings, scope, radius)
        if BUILDING_INTENT.search(question):
            text = "**Tiện ích tòa nhà & dịch vụ**\n\n" + listing_table(listings, scope, ["amenities"]) + "\n\n**Địa điểm bên ngoài**\n\n" + text
        result = AskResult(answer=text, status="ANSWERED", citations=[], request_id=request_id,
                           listing_context=result_context, listing_ids=selected, data_sources=["CORE_NEARBY"])
        return await assistant._advise_main(question, request_id, result, listings, persist,
            nearby_facts=nearby_facts(chosen, radius=radius, profile=(persist or {}).get("renterProfile")))
    except ValueError as error:
        return AskResult(answer=str(error), status="ANSWERED", citations=[], request_id=request_id, listing_context=_context(assistant, scope, persist))
    except (ListingToolError, GenerationUnavailableError, HTTPException):
        return AskResult(answer="Mình chưa đọc được dữ liệu tiện ích xung quanh lúc này. Nhóm phòng bạn đã chọn vẫn được giữ để thử lại.",
                         status="TOOL_UNAVAILABLE", citations=[], request_id=request_id, listing_context=_context(assistant, scope, persist))
