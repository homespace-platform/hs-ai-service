"""Cached POI evidence for an existing shortlist; never contacts a map provider."""
import re
from typing import Any
from homespace_ai.application.listing_context import context_for, current_scope, RECOMMENDATION
from homespace_ai.application.vietnamese_text import fold_text, VietnamesePattern
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.application.listing_presentation import customer_listing_text
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

def radius_for(question: str, history: list[dict[str, Any]], default: int | None = None) -> int:
    for value in [question, *(m.get("content", "") for m in reversed(history) if m.get("role") == "user")]:
        match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(km|kilomet|m|met)\b", fold_text(value))
        if match and (match[2] in {"km", "kilomet"} or re.search(r"ban kinh|pham vi|trong|quanh|khoang cach", fold_text(value)[:match.start()][-30:])):
            radius = round(float(match[1].replace(",", ".")) * (1000 if match[2] in {"km", "kilomet"} else 1))
            if not 100 <= radius <= 5000:
                raise ValueError("Mình hiện hỗ trợ bán kính từ 100 m đến 5 km.")
            return radius
    return nearby_default_radius_m() if default is None else default

async def answer_nearby(assistant, question, history, request_id, arguments):
    scope = current_scope(history)
    ids = arguments.get("listingIds") or scope
    if not ids or any(value not in scope for value in ids):
        return AskResult(answer="Bạn hãy tìm và chọn nhóm phòng trước để mình đối chiếu tiện ích xung quanh.",
                         status="ANSWERED", citations=[], request_id=request_id, listing_context=context_for(scope))
    if len(ids) > 20:
        return AskResult(answer="Bạn chọn tối đa 20 phòng trong một lượt để mình đối chiếu tiện ích nhé.",
                         status="ANSWERED", citations=[], request_id=request_id, listing_context=context_for(scope))
    try:
        radius = radius_for(question, [], arguments.get("radiusMeters"))
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
                return AskResult(answer=text, status="ANSWERED", citations=[], request_id=request_id, listing_context=context_for(scope), data_sources=["CORE_NEARBY"])
            target_id = resolved["arguments"]["nearby"]["placeId"]
        groups = arguments.get("groups") or list(GROUP_LABELS)
        evidence = await assistant.tools.nearby(ids, radius, groups, target_id)
        summaries = evidence["summaries"]
        if any(not s.get("enabled") for s in summaries):
            return AskResult(answer="Dữ liệu tiện ích xung quanh hiện chưa được bật. Mình vẫn có thể tư vấn theo giá, diện tích và tiện nghi trong tin đăng.",
                             status="ANSWERED", citations=[], request_id=request_id, listing_context=context_for(scope))
        result_context = context_for(scope) | {"nearby": {"radiusMeters": radius, "groups": groups, "targetPlaceId": target_id}}
        target_known = target_id is not None and all(s.get("target") for s in summaries)
        complete = all(all(g.get("coverageStatus") == "COMPLETE_PROVIDER_RESPONSE"
                           and (g.get("radiusCoveredMeters") or 0) >= radius
                           for g in s.get("groups", [])) and len(s.get("groups", [])) == len(groups)
                       for s in summaries)
        ranking = bool(target_known or complete)
        if RECOMMENDATION.search(question) and not ranking:
            return AskResult(
                answer="Dữ liệu tiện ích của nhóm phòng này chưa đủ hoặc chưa phủ hết bán kính bạn yêu cầu, nên mình chưa thể chọn phòng tốt hơn theo vị trí một cách đáng tin cậy. "
                       "Bạn vẫn có thể so sánh giá thuê, diện tích và tiện nghi trong tin đăng.",
                status="ANSWERED", citations=[], request_id=request_id, listing_context=result_context)
        if target_known:
            summaries = [s for s in summaries if s["target"].get("withinRadius")]
            summaries.sort(key=lambda s: s["target"]["distanceMeters"])
        elif ranking:
            def score(summary):
                by_group = {g["code"]: g for g in summary["groups"]}
                distances = [by_group[code]["nearest"][0]["distanceMeters"] if by_group[code].get("nearest") else radius * 2
                             for code in groups]
                return sum(distance / (i + 1) for i, distance in enumerate(distances))
            summaries.sort(key=score)
        summaries = summaries[:3]
        if not summaries:
            return AskResult(answer="Các phòng đang chọn không nằm trong bán kính bạn yêu cầu quanh địa điểm đó. Bạn muốn tăng bán kính hay tìm nhóm phòng khác?",
                             status="NO_RESULTS", citations=[], request_id=request_id, listing_context=context_for(scope))
        # Only the selected three receive full details/model synthesis, even with 20 candidates.
        selected = [s["listingId"] for s in summaries]
        listings = [await assistant.tools.detail(value) for value in selected]
        data = {"listings": listings, "nearby": summaries, "scopeOnly": True, "rankingSupported": ranking,
                "radiusMeters": radius, "distanceType": "STRAIGHT_LINE", "recommendationLimit": 3,
                "selectionScope": [{"listingId": value, "position": scope.index(value) + 1} for value in selected],
                "recentUserRequests": [m["content"][:1200] for m in history[-8:] if m.get("role") == "user"],
                "limitations": "POI chưa chứng minh đang hoạt động. Thiếu coverage là chưa biết, không phải không có. Không suy thời gian đi lại hoặc độ ồn."}
        generated = await assistant.generative_client.generate_listing_answer(question=question, evidence=data)
        if any(value not in selected for value in generated.listing_ids):
            raise GenerationUnavailableError("Unknown nearby recommendation", retryable=False)
        text = customer_listing_text(generated.answer)
        text = re.sub(r"\[([^\]]+)\]\(/rent/[0-9a-fA-F-]{36}\)", r"\1", text)
        text = re.sub(r"(?:https?://[^\s/]+)?/rent/[0-9a-fA-F-]{36}", "", text)
        return AskResult(answer=text.strip(), status="ANSWERED", citations=[], request_id=request_id,
                         listing_context=result_context, listing_ids=list(dict.fromkeys(generated.listing_ids))[:3], data_sources=["CORE_NEARBY"])
    except ValueError as error:
        return AskResult(answer=str(error), status="ANSWERED", citations=[], request_id=request_id, listing_context=context_for(scope))
    except (ListingToolError, GenerationUnavailableError):
        return AskResult(answer="Mình chưa đọc được dữ liệu tiện ích xung quanh lúc này. Nhóm phòng bạn đã chọn vẫn được giữ để thử lại.",
                         status="TOOL_UNAVAILABLE", citations=[], request_id=request_id, listing_context=context_for(scope))
