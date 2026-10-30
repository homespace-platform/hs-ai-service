"""Bounded, authoritative Nearby tables and recommendation facts from cached Core evidence."""
from math import isfinite

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
    return f"{place.get('name') or 'Địa điểm đã lưu'} — {distance(place.get('distanceMeters'))}"


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
            lines.append("| Địa điểm đang đối chiếu | " + " | ".join(_cell(_place(by_id[item["id"]]["target"]))
                if by_id[item["id"]].get("target") else "Chưa có dữ liệu" for item in part) + " |")
        for code in codes:
            cells = []
            for item in part:
                group = next((g for g in by_id[item["id"]].get("groups", []) if g.get("code") == code), {})
                nearest = group.get("nearest") or []
                cells.append(_cell(_place(nearest[0]), limit=200) if nearest else "Chưa ghi nhận trong dữ liệu đã lưu")
            lines.append("| " + GROUP_LABELS[code] + " | " + " | ".join(cells) + " |")
        tables.append("\n".join(lines))
    note = f"Trong phạm vi {distance(radius)}, khoảng cách đường chim bay từ từng bài đăng. Nguồn địa điểm: TomTom, đọc từ dữ liệu đã lưu."
    if any(g.get("coverageStatus") != "COMPLETE_PROVIDER_RESPONSE" or (g.get("radiusCoveredMeters") or 0) < radius
           for s in summaries for g in s.get("groups", [])):
        note += " Dữ liệu là mẫu đã thu thập; chưa ghi nhận không có nghĩa khu vực không có tiện ích."
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
            evidence.append((code, GROUP_LABELS[code] + " đã ghi nhận gần nhất", group["nearest"][0]))
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
