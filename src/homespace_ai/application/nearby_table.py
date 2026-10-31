"""Bounded, authoritative Nearby tables and recommendation facts from cached Core evidence."""
from math import isfinite
from html import escape
import re

from homespace_ai.application.listing_table import COLUMN, COLUMNS_PER_TABLE, _cell, _header

GROUP_LABELS = {
    "EDUCATION": "Giáo dục", "HEALTHCARE": "Y tế", "SHOPPING": "Mua sắm", "FOOD_DRINK": "Ăn uống",
    "FINANCE": "Ngân hàng", "TRANSPORT": "Đi lại", "RECREATION": "Giải trí", "SPORTS": "Thể thao",
}


def distance(value) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value) or value < 0:
        return "Chưa rõ khoảng cách"
    return f"{value / 1000:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " km" if value >= 1000 else f"{round(value)} m"


def _place(place: dict) -> str:
    return place.get('name') or 'Địa điểm trong khu vực'


def _place_cell(place: dict) -> str:
    return re.sub(r"([\\`*{}\[\]()!_>])", r"\\\1", escape(_cell(_place(place), limit=200), quote=False))


def render(summaries: list[dict], listings: list[dict], scope: list[str], radius: int) -> str:
    by_id = {row["listingId"]: row for row in summaries}
    codes = [code for code in GROUP_LABELS if any(g.get("code") == code for s in summaries for g in s.get("groups", []))]
    tables = []
    for start in range(0, len(listings), COLUMNS_PER_TABLE):
        part = listings[start:start+COLUMNS_PER_TABLE]
        headers = [_header(item, f"{COLUMN.get(item.get('category'), 'Tin')} {scope.index(item['id'])+1}") for item in part]
        lines = ["| Tiện ích xung quanh | " + " | ".join(headers) + " |",
                 "| --- | " + " | ".join("---" for _ in headers) + " |"]
        if any(by_id[item["id"]].get("target") for item in part):
            lines.append("| Địa điểm đang đối chiếu | " + " | ".join(_place_cell(by_id[item["id"]]["target"])
                if by_id[item["id"]].get("target") else "Chưa có dữ liệu" for item in part) + " |")
        for code in codes:
            cells = []
            for item in part:
                group = next((g for g in by_id[item["id"]].get("groups", []) if g.get("code") == code), {})
                nearest = group.get("nearest") or []
                # Escape each source name before adding trusted line breaks. Never let
                # provider markup enter the Markdown table or truncate away POIs2–5.
                cells.append("<br>".join(f"{index+1}. {_place_cell(p)}" for index,p in enumerate(nearest[:5]))
                             if nearest else "Chưa có thông tin cho nhóm này")
            lines.append("| " + GROUP_LABELS[code] + " | " + " | ".join(cells) + " |")
        tables.append("\n".join(lines))
    note = f"Các địa điểm trên nằm trong phạm vi {distance(radius)} quanh từng phòng; mỗi nhóm hiển thị tối đa 5 địa điểm để bạn dễ so sánh."
    return "\n\n".join(tables + [note])


def facts(summaries: list[dict], *, radius: int, profile: dict | None = None) -> dict:
    """Only positive, covered evidence. No counts-as-quality or inferred school level."""
    result = {}
    for summary in summaries:
        row = {}
        evidence = []
        target = summary.get("target")
        if target and target.get("withinRadius"):
            evidence.append(("target", "Địa điểm đang đối chiếu", target))
        for group in summary.get("groups", []):
            code = group.get("code")
            if (code not in GROUP_LABELS or not group.get("nearest") or (group.get("radiusCoveredMeters") or 0) < radius
                    or group.get("coverageStatus") not in {"COMPLETE_PROVIDER_RESPONSE", "LIMITED_PROVIDER_RESPONSE"}):
                continue
            # A generic education group may be a kindergarten, not the student's university.
            if code == "EDUCATION" and (profile or {}).get("persona") == "STUDENT":
                continue
            evidence.append((code, GROUP_LABELS[code] + " quanh phòng", group["nearest"][0]))
        for code, label, place in evidence:
            meters = place.get("distanceMeters")
            if not isinstance(meters, (int, float)) or isinstance(meters, bool) or not isfinite(meters) or not 0 <= meters <= radius:
                continue
            path = "nearby." + code + ".distanceMeters"
            row[path] = {"value": meters, "display": _place(place), "label": label, "unit": None,
                         "direction": "LOWER", "leaf": "distanceMeters", "code": path,
                         "comparisonKey": place.get("placeId") if code == "target" else code}
        result[summary["listingId"]] = row
    return result
