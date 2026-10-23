"""Near-budget "standout" suggestion: one listing slightly above budget that is clearly better on space.

Pure helpers: the search itself still goes through Core; the model never filters or invents prices.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

STRETCH_RATE = Decimal("0.075")  # 4.000.000 -> +300.000
STRETCH_MAX = Decimal(500_000)
STRETCH_STEP = Decimal(50_000)
MIN_AREA_GAIN = Decimal("1.1")  # the standout must be >= 10% larger than the largest in-budget room


def _number(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def budget_of(filters: list[dict[str, Any]]) -> Decimal | None:
    """Upper price bound of the plan (price_amount lt/lte), or None when the customer gave no budget."""
    bounds = [_number(item.get("value")) for item in filters or []
              if isinstance(item, dict) and item.get("field") == "price_amount" and item.get("op") in {"lt", "lte"}]
    bounds = [value for value in bounds if value is not None and value > 0]
    return min(bounds) if bounds else None


def tolerance(budget: Decimal) -> Decimal:
    raw = (budget * STRETCH_RATE / STRETCH_STEP).to_integral_value() * STRETCH_STEP
    return max(STRETCH_STEP, min(STRETCH_MAX, raw))


def stretch_filters(filters: list[dict[str, Any]], budget: Decimal) -> list[dict[str, Any]]:
    """Same criteria, but price in (budget, budget + tolerance]; the area minimum is kept."""
    rest = [item for item in filters if not (isinstance(item, dict) and item.get("field") == "price_amount")]
    return rest + [{"field": "price_amount", "op": "gt", "value": str(int(budget))},
                   {"field": "price_amount", "op": "lte", "value": str(int(budget + tolerance(budget)))}]


def _amount(item: dict[str, Any]) -> Decimal | None:
    return _number((item.get("pricing") or {}).get("amount"))


def pick_standout(candidates: list[dict[str, Any]], in_budget: list[dict[str, Any]], budget: Decimal) -> dict[str, Any] | None:
    """Largest candidate within tolerance that is meaningfully bigger than every in-budget room."""
    limit = budget + tolerance(budget)
    best_area = max([a for a in (_number(item.get("areaM2")) for item in in_budget) if a is not None], default=Decimal(0))
    shown = {item.get("id") for item in in_budget}
    ranked = []
    for item in candidates:
        price, area = _amount(item), _number(item.get("areaM2"))
        if item.get("id") in shown or price is None or area is None or not budget < price <= limit:
            continue
        if best_area and area < best_area * MIN_AREA_GAIN:
            continue
        ranked.append((area, -price, item))
    return max(ranked, key=lambda row: (row[0], row[1]))[2] if ranked else None


def _money(value: Decimal) -> str:
    return f"{int(value):,}".replace(",", ".") + " đ"


def _area(value: Decimal) -> str:
    return f"{value.normalize():f} m²"


def stretch_note(item: dict[str, Any], in_budget: list[dict[str, Any]], budget: Decimal, position: int) -> str:
    price, area = _amount(item), _number(item.get("areaM2"))
    over = price - budget
    areas = [a for a in (_number(room.get("areaM2")) for room in in_budget) if a is not None]
    text = (f"**Gợi ý ngoài tiêu chí – Tin {position}:** giá {_money(price)}/tháng, cao hơn ngân sách {_money(over)} "
            f"nhưng rộng {_area(area)}")
    if areas:
        text += f" (phòng rộng nhất trong tiêu chí chỉ {_area(max(areas))})"
    return text + ". Nếu có thể linh hoạt thêm một chút, đây là lựa chọn đáng cân nhắc; đây không phải phòng đúng ngân sách."


def no_standout_note(budget: Decimal) -> str:
    upper = budget + tolerance(budget)
    return (f"Mình cũng đã xem thêm các phòng giá đến {_money(upper)} (nới {_money(tolerance(budget))}): "
            "hiện chưa có phòng nào rộng hơn đáng kể so với các phòng trên nên chưa có gợi ý ngoài tiêu chí.")


def no_standout_note(budget: Decimal) -> str:
    upper = budget + tolerance(budget)
    return (f"Mình cũng đã xem thêm các phòng giá đến {_money(upper)} (nới {_money(tolerance(budget))}): "
            "hiện chưa có phòng nào rộng hơn đáng kể so với các phòng trên nên chưa có gợi ý ngoài tiêu chí.")
