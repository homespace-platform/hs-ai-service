"""Explain a stored main choice using ALL fresh shortlist facts, never old prose/prices."""
import json

from homespace_ai.application.listing_table import COLUMN, COLUMNS_PER_TABLE, _header
from homespace_ai.application.nearby_table import _place_cell
from homespace_ai.application import listing_value_advice as value
from homespace_ai.clients.generative import GenerationUnavailableError
from homespace_ai.tools.listings import ListingToolError


async def explain(question, reference, listings, criteria, scope, catalog, generator, nearby_facts=None):
    selected = reference.get("listingId")
    by_id = {item["id"]: item for item in listings}
    if selected not in by_id:
        return None
    today = None
    if criteria.get("requestToday"):
        from datetime import date
        today = date.fromisoformat(criteria["requestToday"])
    nearby_only = reference.get("focus") == "NEARBY" or bool(reference.get("fields")) and all(
        path.startswith("nearby.") for path in reference["fields"])
    from homespace_ai.application.answer_context import scoped_facts
    focus = "NEARBY" if nearby_only else reference.get("focus") or "OVERALL"
    facts = {item["id"]: {} if nearby_only else scoped_facts(value.recommendation_facts(item, criteria, catalog, today), focus) for item in listings}
    for identifier,row in (nearby_facts or {}).items():
        if identifier in facts:
            facts[identifier].update(row)
    others = {identifier: row for identifier,row in facts.items() if identifier != selected}
    informative = [path for path in facts[selected] if not value.common_reason(path, facts[selected], others)
                   and not value._ignored(facts[selected][path], criteria)]
    evidence = {"selectedId": selected, "confirmedCriteria": criteria,
        "listings": [{"id": item["id"], "factColumn": index} for index,item in enumerate(listings)],
        "factMatrix": value._judge_matrix(list(facts.values())),
        "selectedComparisons": value._verified_comparisons(facts[selected], others),
        "otherListingIds": list(others), "previousReasonFields": reference.get("fields", []),
        "informativeFields": informative, "recommendationFocus": focus}
    if len(json.dumps(evidence,ensure_ascii=False,default=str).encode()) > 120000:
        raise ListingToolError("Main explanation evidence is too large")
    valid = set(facts[selected])
    fallback = [field for field in reference.get("fields", []) if field in informative][:3]
    plan = {"reasons": fallback, "tradeoffs": []}
    if hasattr(generator,"generate_main_explanation"):
        try:
            plan = await generator.generate_main_explanation(question,evidence)
        except GenerationUnavailableError:
            pass  # Still explain fresh verified facts if optional language reasoning is unavailable.
    if (not isinstance(plan,dict) or any(not isinstance(plan.get(key),list) or len(plan[key])>limit
        or any(field not in valid for field in plan[key]) for key,limit in (("reasons",3),("tradeoffs",2)))):
        raise ListingToolError("Invalid main explanation fields")

    def link(identifier):
        item = by_id[identifier]
        return f"[{COLUMN.get(item.get('category'),'Tin')} {scope.index(identifier)+1}](/rent/{identifier})"

    def comparisons(path):
        current=facts[selected][path]
        rows=[]
        for identifier,other in others.items():
            if path not in other:
                continue
            change=value._comparison(current,other[path])
            if change is not None:
                rows.append((identifier,other[path],change))
        return rows

    # Keep the original verified rationale, then deepen it with related differences.
    fields=list(dict.fromkeys(fallback+plan["reasons"]))[:3]
    fields=list(dict.fromkeys(fields+plan["tradeoffs"]))
    # An appreciably higher rent is a useful, verified tradeoff even if the judge omits it.
    if not nearby_only and "pricing.amount" in valid and any(change<0 for _,_,change in comparisons("pricing.amount")) and "pricing.amount" not in fields:
        fields.append("pricing.amount")
    rendered=[]
    for path in fields[:5]:
        current=facts[selected][path]
        if path not in informative:
            continue
        rows=comparisons(path)
        better=[row for row in rows if row[2]==1 or row[2]==2 and value._context_improves(current,row[1],criteria)]
        worse=[row for row in rows if row[2]==-1]
        same=[row for row in rows if row[2]==0]
        if value.shared_use(current):
            note="Điểm cần cân nhắc: dùng chung, không phải lợi thế riêng tư."
        elif better and not worse and not same and len(rows)==len(others):
            note={"areaM2":"Không gian rộng hơn các phòng còn lại.", "pricing.amount":"Tiền thuê thấp hơn các phòng còn lại.",
                  "nearby.target.distanceMeters":"Gần địa điểm bạn quan tâm hơn các phòng còn lại."}.get(path,
                  "Địa điểm ở gần hơn trong nhóm đã đối chiếu." if path.startswith("nearby.") else "Thuận lợi hơn cả nhóm ở tiêu chí này.")
        elif worse:
            note="Điểm cần cân nhắc: có lựa chọn tốt hơn; không vượt trội cả nhóm."
        elif better:
            note="Nhỉnh hơn một số phòng, nhưng không vượt trội cả nhóm."
        elif any(row[2]==2 for row in rows):
            note="Khác nhau về cách sử dụng; cân nhắc theo thói quen sinh hoạt."
        elif same:
            note="Tương đương một số lựa chọn, không phải lợi thế riêng."
        else:
            note="Các phòng còn lại chưa có đủ thông tin để đối chiếu."
        rendered.append((path,note))
    lead=f"**Mình đã đối chiếu lại {link(selected)} với các phòng còn lại theo " + ("tiện ích xung quanh:" if nearby_only else "nhu cầu bạn đã chia sẻ:") + "**"
    if not rendered:
        text=f"Mình chưa có đủ khác biệt đã xác minh để khẳng định {link(selected)} nổi bật hơn các phòng còn lại ở nội dung này."
        if len(listings)>1:
            for start in range(0,len(listings),COLUMNS_PER_TABLE):
                part=listings[start:start+COLUMNS_PER_TABLE]
                headers=[_header(item,f"{COLUMN.get(item.get('category'),'Tin')} {scope.index(item['id'])+1}") for item in part]
                text += "\n\n| Cơ sở đối chiếu | " + " | ".join(headers) + " |\n| --- | " + " | ".join("---" for _ in headers) + " |\n| Thông tin khác biệt | " + " | ".join("Chưa đủ bằng chứng" for _ in headers) + " |"
        return text
    tables=[]
    for start in range(0,len(listings),COLUMNS_PER_TABLE):
        part=listings[start:start+COLUMNS_PER_TABLE]
        headers=[_header(item,f"{COLUMN.get(item.get('category'),'Tin')} {scope.index(item['id'])+1}" + (" · đang chọn" if item['id']==selected else "")) for item in part]
        lines=["| Tiêu chí đối chiếu | " + " | ".join(headers) + " | Đánh giá |", "| --- | " + " | ".join("---" for _ in headers) + " | --- |"]
        for path,note in rendered:
            cells=[_place_cell({"name":value._display(facts[item['id']][path])}) if path in facts[item['id']] else "Chưa có thông tin" for item in part]
            label=_place_cell({"name":facts[selected][path]['label']})
            lines.append("| " + label + " | " + " | ".join(cells) + " | " + note + " |")
        tables.append("\n".join(lines))
    return lead+"\n\n"+"\n\n".join(tables)
