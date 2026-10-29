"""Cached POI evidence for an existing shortlist; never contacts a map provider.

Hard criteria (stored and newly stated) are re-checked by Core inside the displayed scope
before any ranking, so the top three are chosen only among rooms that still qualify.
"""
import re
from typing import Any
from homespace_ai.application.listing_context import context_for, current_scope, RECOMMENDATION
from homespace_ai.application.vietnamese_text import fold_text, VietnamesePattern
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.application.listing_presentation import customer_listing_text
from homespace_ai.application.listing_table import strip_images
from homespace_ai.application.listing_catalog import CatalogError, SearchCatalog
from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.renter_criteria import normalize
from homespace_ai.clients.generative import GenerationUnavailableError
from homespace_ai.tools.listings import ListingToolError
from homespace_ai.core.business_properties import nearby_default_radius_m

NEARBY_INTENT = VietnamesePattern(
    r"quét.{0,40}(?:xung quanh|tiện ích)|bán kính|khoảng cách (?:đến|tới)|tiện ích (?:xung quanh|lân cận)|"
    r"(?:gần|quanh|xung quanh).{0,60}(?:trường|đại học|siêu thị|bệnh viện|công viên|chợ|gym|nơi làm)")
GROUP_LABELS = {
    "EDUCATION": "giáo dục", "HEALTHCARE": "y tế", "SHOPPING": "mua sắm", "FOOD_DRINK": "ăn uống",
    "FINANCE": "ngân hàng", "TRANSPORT": "đi lại", "RECREATION": "giải trí", "SPORTS": "thể thao",
}
RECOMMENDATION_LIMIT = 3


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
        target_id = arguments.get("targetPlaceId")
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
        target_known = target_id is not None and all(s.get("target") for s in summaries)
        complete = all(all(g.get("coverageStatus") == "COMPLETE_PROVIDER_RESPONSE"
                           and (g.get("radiusCoveredMeters") or 0) >= radius
                           for g in s.get("groups", [])) and len(s.get("groups", [])) == len(groups)
                       for s in summaries)
        limited = any(g.get("coverageStatus") == "LIMITED_PROVIDER_RESPONSE" for s in summaries for g in s.get("groups", []))
        usable_sample = all(all(g.get("coverageStatus") in {"COMPLETE_PROVIDER_RESPONSE", "LIMITED_PROVIDER_RESPONSE"}
                                and (g.get("radiusCoveredMeters") or 0) >= radius for g in s.get("groups", []))
                            and len(s.get("groups", [])) == len(groups) for s in summaries)
        # A missing group in the first 100 is unknown. Compare positive evidence shared by all candidates.
        common_groups = [code for code in groups if all(any(g.get("code") == code and g.get("nearest")
                                                           for g in s.get("groups", [])) for s in summaries)]
        comparison_groups = groups if complete else common_groups
        ranking = bool(target_known or complete or (usable_sample and common_groups))
        if RECOMMENDATION.search(question) and not ranking:
            return AskResult(
                answer="Dữ liệu tiện ích của nhóm phòng này chưa đủ hoặc chưa phủ hết bán kính bạn yêu cầu, nên mình chưa thể chọn phòng tốt hơn theo vị trí một cách đáng tin cậy. "
                       "Bạn vẫn có thể so sánh giá thuê, diện tích và tiện nghi trong tin đăng.",
                status="ANSWERED", citations=[], request_id=request_id, listing_context=result_context)
        if target_known:
            summaries = [s for s in summaries if s["target"].get("withinRadius")]
            summaries.sort(key=lambda s: (s["target"]["distanceMeters"], scope.index(s["listingId"])))
        elif ranking:
            def score(summary):
                by_group = {g["code"]: g for g in summary["groups"]}
                distances = [by_group[code]["nearest"][0]["distanceMeters"] if by_group[code].get("nearest") else radius * 2
                             for code in comparison_groups]
                return (sum(distance / (i + 1) for i, distance in enumerate(distances)), scope.index(summary["listingId"]))
            summaries.sort(key=score)
        if not summaries:
            return AskResult(answer="Các phòng đang chọn không nằm trong bán kính bạn yêu cầu quanh địa điểm đó. Bạn muốn tăng bán kính hay tìm nhóm phòng khác?",
                             status="NO_RESULTS", citations=[], request_id=request_id, listing_context=_context(assistant, scope, persist))
        # Only the selected rooms receive full details; a room hidden since the search gives its place to the next one.
        chosen, listings = [], []
        for summary in summaries:
            if len(chosen) == RECOMMENDATION_LIMIT:
                break
            try:
                listings.append(await assistant.tools.detail(summary["listingId"]))
                chosen.append(summary)
            except ListingToolError as error:
                if error.status_code in {404, 410} or str(error) == "Listing is no longer public":
                    continue
                raise
        if not chosen:
            return AskResult(answer="Các phòng phù hợp vừa đổi trạng thái và không còn hiển thị công khai. Bạn muốn tìm lại theo các điều kiện đã lưu không?",
                             status="NO_RESULTS", citations=[], request_id=request_id, listing_context=_context(assistant, [], persist))
        selected = [s["listingId"] for s in chosen]
        data = {"listings": listings, "nearby": chosen, "scopeOnly": True, "rankingSupported": ranking,
                "radiusMeters": radius, "distanceType": "STRAIGHT_LINE", "recommendationLimit": RECOMMENDATION_LIMIT,
                "eligibleInScope": len(eligible), "scopeSize": len(ids),
                "limitedSelection": limited, "comparisonGroups": comparison_groups,
                "selectionScope": [{"listingId": value, "position": scope.index(value) + 1} for value in selected],
                "recentUserRequests": [m["content"][:1200] for m in history[-8:] if m.get("role") == "user"],
                "limitations": "Mỗi listing lấy tối đa 100 địa điểm từ một request. Khi limitedSelection=true, chỉ so sánh theo comparisonGroups có bằng chứng; nêu rõ giới hạn mẫu và không kết luận nhóm 0 kết quả là không có tiện ích. POI chưa chứng minh đang hoạt động. Không suy thời gian đi lại hoặc độ ồn. Chỉ các phòng trong listings đã được Core kiểm tra lại đủ tiêu chí."}
        generated = await assistant.generative_client.generate_listing_answer(question=question, evidence=data)
        if any(value not in selected for value in generated.listing_ids):
            raise GenerationUnavailableError("Unknown nearby recommendation", retryable=False)
        text = customer_listing_text(generated.answer)
        text = re.sub(r"\[([^\]]+)\]\(/rent/[0-9a-fA-F-]{36}\)", r"\1", text)
        text = re.sub(r"(?:https?://[^\s/]+)?/rent/[0-9a-fA-F-]{36}", "", strip_images(text))
        return AskResult(answer=text.strip(), status="ANSWERED", citations=[], request_id=request_id,
                         listing_context=result_context, listing_ids=list(dict.fromkeys(generated.listing_ids))[:RECOMMENDATION_LIMIT], data_sources=["CORE_NEARBY"])
    except ValueError as error:
        return AskResult(answer=str(error), status="ANSWERED", citations=[], request_id=request_id, listing_context=_context(assistant, scope, persist))
    except (ListingToolError, GenerationUnavailableError):
        return AskResult(answer="Mình chưa đọc được dữ liệu tiện ích xung quanh lúc này. Nhóm phòng bạn đã chọn vẫn được giữ để thử lại.",
                         status="TOOL_UNAVAILABLE", citations=[], request_id=request_id, listing_context=_context(assistant, scope, persist))
