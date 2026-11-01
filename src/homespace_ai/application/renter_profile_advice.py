"""Continue a shortlist with persona-relevant, verified room facts and cached nearby places.

One bounded Core summary reads the whole displayed group. No provider scan, global search,
boundary lookup, persona-derived hard filter or stale listing snapshot is used.
"""
import asyncio
from copy import deepcopy
from math import isfinite

from fastapi import HTTPException

from homespace_ai.application import listing_value_advice as value, renter_guidance as guidance
from homespace_ai.application.listing_catalog import CatalogError, SearchCatalog
from homespace_ai.application.listing_table import COLUMN, COLUMNS_PER_TABLE, _cell, _header, _price
from homespace_ai.application.nearby_table import GROUP_LABELS, _place_cell, facts as nearby_facts
from homespace_ai.application.nearby_listing_advice import _eligible
from homespace_ai.application.use_cases.ask_use_cases import AskResult
from homespace_ai.clients.generative import GenerationUnavailableError
from homespace_ai.core.business_properties import value_advice_timeout_seconds
from homespace_ai.tools.listings import ListingToolError

PERSONA_LABELS = {"FAMILY": "gia đình", "STUDENT": "sinh viên", "WORKING": "người đi làm", "REMOTE": "người làm việc từ xa"}
PERSONA_GROUPS = {
    "FAMILY": ["SHOPPING", "HEALTHCARE", "RECREATION", "EDUCATION"],
    "STUDENT": ["EDUCATION", "TRANSPORT", "FOOD_DRINK", "SHOPPING"],
    "WORKING": ["TRANSPORT", "SHOPPING", "FOOD_DRINK", "HEALTHCARE"],
    "REMOTE": ["SHOPPING", "FOOD_DRINK", "RECREATION", "HEALTHCARE"],
    "OTHER": ["EDUCATION", "HEALTHCARE", "SHOPPING", "FOOD_DRINK", "FINANCE", "TRANSPORT", "RECREATION"],
}


def _phrases(reasons, fallback):
    return "; ".join(value.reason_text(reason) for reason in reasons) or fallback


def _building_amenities(item):
    # Labels are source content, not Markdown/HTML or internal amenity codes.
    names = [a["name"] for a in item.get("amenities") or [] if a.get("name")]
    return ", ".join(_place_cell({"name": name}) for name in names[:8]) or "Tin chưa cung cấp thông tin"


def observed_nearby(summaries, groups, radius):
    """Known places remain useful with partial coverage; this is NOT a distance-ranking fact."""
    def in_radius(place):
        distance = place.get("distanceMeters")
        return (bool(place.get("name")) and isinstance(distance, (int, float)) and not isinstance(distance, bool)
                and isfinite(distance) and 0 <= distance <= radius)

    result = {}
    for summary in summaries:
        row = {"groups": {}}
        for group in summary.get("groups", []):
            if group.get("code") not in groups:
                continue
            names = list(dict.fromkeys(p["name"] for p in group.get("nearest", [])[:5] if in_radius(p)))
            if names:
                row["groups"][group["code"]] = {"names": names, "coverageStatus": group.get("coverageStatus")}
        target = summary.get("target") or {}
        if target.get("withinRadius") and in_radius(target):
            row["target"] = {"name": target["name"]}
        result[summary["listingId"]] = row
    return result


def selected_nearby_text(observed, identifier, groups, persona):
    """Mention known places as context, never as a unique advantage or proof of a commute."""
    row = observed.get(identifier, {})
    selected = [row["target"]["name"]] if row.get("target") else []
    for code in groups:
        if code == "EDUCATION" and persona == "STUDENT":
            continue  # A generic school is not proof of proximity to the student's university.
        selected += row.get("groups", {}).get(code, {}).get("names", [])[:1]
    names = list(dict.fromkeys(selected))[:2]
    return "Quanh lựa chọn này có " + " và ".join(f"**{_place_cell({'name': name})}**" for name in names) + " để bạn cân nhắc thêm về sinh hoạt, đi lại." if names else ""


def render_profile_table(listings, scope, assessments, summaries, groups, persona):
    """A focused assessment, not the original image/basic-field search table."""
    assessed = {row["listingId"]: row for row in assessments}
    nearby = {row["listingId"]: row for row in summaries}
    tables = []
    for start in range(0, len(listings), COLUMNS_PER_TABLE):
        part = listings[start:start+COLUMNS_PER_TABLE]
        headers = [_header(item, f"{COLUMN.get(item.get('category'), 'Tin')} {scope.index(item['id'])+1}") for item in part]
        label = f"Đối chiếu cho {PERSONA_LABELS[persona]}" if persona in PERSONA_LABELS else "Đối chiếu theo nhu cầu của bạn"
        lines = [f"| {label} | " + " | ".join(headers) + " |",
                 "| --- | " + " | ".join("---" for _ in part) + " |"]
        rows = [("Tiền thuê", [_price(item) for item in part])]
        rows.append(("Không gian sinh hoạt", [
            "; ".join(text for text in [f"{value.formatted(item['areaM2'])} m²" if item.get("areaM2") is not None else None,
                next((f"Tối đa {item[detail]['maxOccupants']} người" for detail in ("roomDetail", "apartmentDetail", "houseDetail")
                      if (item.get(detail) or {}).get("maxOccupants") is not None), None)] if text)
            or "Chưa có thông tin" for item in part]))
        rows.append(("Điểm phù hợp để cân nhắc", [_phrases(assessed.get(item["id"], {}).get("strengths", []),
            "Chưa xác định được lợi thế riêng so với nhóm") for item in part]))
        rows.append(("Điểm cần cân nhắc", [_phrases(assessed.get(item["id"], {}).get("considerations", []),
            "Chưa đủ thông tin để nêu thêm đánh đổi") for item in part]))
        rows.append(("Tiện ích trong tòa nhà", [_building_amenities(item) for item in part]))
        lines += ["| " + label + " | " + " | ".join(_cell(text, limit=550) for text in cells) + " |" for label, cells in rows]
        if any(nearby.get(item["id"], {}).get("target") for item in part):
            cells = [_place_cell(nearby[item["id"]]["target"]) if nearby.get(item["id"], {}).get("target")
                     else "Chưa có thông tin về địa điểm này" for item in part]
            lines.append("| Địa điểm bạn quan tâm | " + " | ".join(cells) + " |")
        for code in groups:
            cells = []
            for item in part:
                group = next((g for g in nearby.get(item["id"], {}).get("groups", []) if g.get("code") == code), {})
                places = group.get("nearest") or []
                cells.append("<br>".join(f"{n}. {_place_cell(place)}" for n,place in enumerate(places[:5], 1))
                             or "Chưa có thông tin cho nhóm này")
            lines.append(f"| {GROUP_LABELS[code]} quanh phòng | " + " | ".join(cells) + " |")
        tables.append("\n".join(lines))
    return "\n\n".join(tables)


async def answer_profile(assistant, question, history, request_id, scope, state):
    """Keep confirmed criteria/ordinals and use the new persona as advice context only."""
    eligible, _, state = await _eligible(assistant, question, scope, state)
    if not eligible:
        return AskResult(answer="Các phòng vừa xem hiện không còn khớp đủ điều kiện đã chốt. Bạn muốn mình tìm nhóm phòng khác theo các điều kiện này không?",
            status="NO_RESULTS", citations=[], request_id=request_id, listing_context=assistant.context([], state))
    listings, _ = await assistant._details(eligible, request_id)
    if not listings:
        return AskResult(answer="Các phòng vừa xem hiện không còn hiển thị công khai. Bạn muốn tìm lại theo các tiêu chí đã lưu không?",
            status="NO_RESULTS", citations=[], request_id=request_id, listing_context=assistant.context([], state))
    ids = [item["id"] for item in listings]
    profile = (state or {}).get("renterProfile") or {}
    persona = profile.get("persona")
    groups = profile.get("adviceGroups") or PERSONA_GROUPS.get(persona, PERSONA_GROUPS["OTHER"])
    radius = assistant.settings.nearby_default_radius_m
    target = ((state or {}).get("target") or {}).get("placeId")
    summaries, nearby_note = [], None
    if getattr(assistant.settings, "nearby_query_enabled", False):
        try:
            evidence = await assistant.tools.nearby(ids, radius, groups, target)
            summaries = [s for s in evidence["summaries"] if s.get("enabled")]
            if len(summaries) != len(ids):
                nearby_note = "Một số phòng chưa có đủ thông tin về tiện ích xung quanh; mình không dùng phần thiếu để kết luận phòng kém hơn."
        except (ListingToolError, HTTPException):
            nearby_note = "Mình chưa đọc được tiện ích xung quanh lúc này; phần tư vấn dưới đây chỉ dựa vào thông tin phòng đã kiểm tra."
    else:
        nearby_note = "Hiện chưa có thông tin tiện ích xung quanh để bổ sung cho nhóm phòng này."
    location_facts = nearby_facts(summaries, radius=radius, profile=profile)
    observed = observed_nearby(summaries, groups, radius)
    advice = None
    try:
        async with asyncio.timeout(value_advice_timeout_seconds()):
            if assistant.catalog is None:
                assistant.catalog = SearchCatalog(await assistant.tools.catalog())
            advice = await value.evaluate(question, state | {"requestToday": assistant.today().isoformat()}, listings, [], {},
                assistant.generative_client, assistant.catalog, recommend_main=True, nearby_facts=location_facts,
                profile_comparison=True, observed_nearby=observed)
    except (CatalogError, ListingToolError, GenerationUnavailableError, HTTPException, TimeoutError, ValueError, TypeError, KeyError) as error:
        assistant._log_failure(request_id, "profile_comparison", error)
    intro = ("Theo hoàn cảnh và nhu cầu bạn vừa chia sẻ, mình đối chiếu lại nhóm phòng đang xem cùng các tiện ích xung quanh:"
             if profile.get("description") else f"Với nhu cầu thuê cho {PERSONA_LABELS.get(persona, 'bạn')}, mình đối chiếu lại nhóm phòng vừa xem theo sinh hoạt và tiện ích xung quanh:")
    text = intro + "\n\n" + render_profile_table(listings, scope, (advice or {}).get("profileAssessments", []), summaries, groups, persona)
    if summaries:
        text += f"\n\nCác địa điểm được liệt kê trong phạm vi {radius / 1000:g} km quanh từng phòng."
    if nearby_note:
        text += "\n\n" + nearby_note
    context = assistant.context(scope, state)
    if summaries:
        context["nearby"] = {"radiusMeters": radius, "groups": groups, "targetPlaceId": target}
    main = (advice or {}).get("main")
    if main:
        text += "\n\n" + value.render_main(main, listings, scope)
        location_note = selected_nearby_text(observed, main["listingId"], groups, persona)
        if location_note:
            text += " " + location_note
        context["mainRecommendation"] = {"listingId": main["listingId"], "fields": [r["field"] for r in main["reasons"]], "focus": "PROFILE"}
    else:
        text += "\n\nMình chưa có đủ cơ sở để chọn một phòng nổi bật hơn cho nhu cầu này."
    followup = guidance.followup_question(state)
    if followup:
        text += "\n\n" + followup
        state = deepcopy(state)
        state["guidanceAsked"] = list(dict.fromkeys(state.get("guidanceAsked", []) + [followup]))[-6:]
        context = assistant.context(scope, state) | {k:v for k,v in context.items() if k in {"mainRecommendation", "nearby"}}
    from homespace_ai.application.answer_context import AnswerContext
    return AskResult(answer=text, status="ANSWERED", citations=[], request_id=request_id,
        listing_context=context, listing_ids=ids, data_sources=["CORE_NEARBY"] if summaries else [],
        answer_context=AnswerContext("LISTING", "PROFILE", ["PROFILE"] + (["NEARBY"] if summaries else []), scope).metadata())
