"""Explain a stored main choice using ALL fresh shortlist facts, never old prose/prices."""
import json

from homespace_ai.application.listing_table import COLUMN
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
    facts = {item["id"]: value.recommendation_facts(item, criteria, catalog, today) for item in listings}
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
        "informativeFields": informative}
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

    bullets=[]
    fields=list(dict.fromkeys(plan["reasons"]+plan["tradeoffs"]))
    # An appreciably higher rent is a useful, verified tradeoff even if the judge omits it.
    if "pricing.amount" in valid and any(change<0 for _,_,change in comparisons("pricing.amount")) and "pricing.amount" not in fields:
        fields.append("pricing.amount")
    for path in fields[:5]:
        current=facts[selected][path]
        if path not in informative:
            continue
        rows=comparisons(path)
        better=[row for row in rows if row[2]==1 or row[2]==2 and value._context_improves(current,row[1],criteria)]
        worse=[row for row in rows if row[2]==-1]
        same=[row for row in rows if row[2]==0]
        prefix="**Điểm cần cân nhắc:** " if worse or value.shared_use(current) else ""
        statement=prefix+"Phòng này "+value.fact_phrase(path,current)
        contrast="; ".join(f"{link(identifier)} {value.fact_phrase(path,other)}" for identifier,other,_ in rows[:3])
        if value.shared_use(current):
            bullets.append(f"- {statement}. Đối chiếu với các lựa chọn còn lại: {contrast}. Đây là hạng mục dùng chung, không phải lợi thế về sự riêng tư.")
        elif better and not worse and not same and len(rows)==len(others):
            benefit={"areaM2":"Không gian rộng hơn các phòng còn lại", "pricing.amount":"Tiền thuê thấp hơn các phòng còn lại"}.get(path,"Đây là điểm thuận lợi hơn so với các phòng còn lại")
            bullets.append(f"- {statement}. {benefit} ({contrast}).")
        elif better:
            # Mixed comparisons must show peers that are equal/better too, not hide them.
            bullets.append(f"- {statement}. Đối chiếu cùng tiêu chí: {contrast}. Phòng này có điểm phù hợp hơn một số lựa chọn, nhưng không vượt trội cả nhóm.")
        elif worse:
            bullets.append(f"- {statement}; {contrast}. Các lựa chọn này có mức tốt hơn hoặc tương đương ở tiêu chí này.")
        elif any(row[2]==2 for row in rows):
            bullets.append(f"- {statement}. Đối chiếu cùng tiêu chí: {contrast}. Sự khác biệt này cần cân nhắc theo thói quen sinh hoạt của bạn.")
        elif same:
            bullets.append(f"- {statement}. {contrast}. Đây là điểm tương đương, không phải lợi thế riêng của phòng được chọn.")
        else:
            bullets.append(f"- {statement}. Các phòng còn lại chưa có đủ thông tin để đối chiếu mục này.")
    if not bullets:
        return f"Mình chưa có đủ dữ kiện để giải thích vì sao {link(selected)} nổi bật hơn các phòng còn lại. Bạn ưu tiên giá thuê, không gian hay tiện nghi sinh hoạt hơn?"
    return f"**Mình đã đối chiếu lại {link(selected)} với các phòng còn lại theo nhu cầu bạn đã chia sẻ:**\n\n"+"\n\n".join(bullets)
