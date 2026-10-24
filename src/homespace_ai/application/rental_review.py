"""Deterministic comparison of competing PENDING rental requests, from the landlord's point of view.

The ranking is computed here from Core data with fixed, published weights. The language model never decides the
winner; it may only explain or answer follow-up questions about this result.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

# Business criteria only. Nothing about the renter as a person (name, gender, origin...) is scored.
WEIGHTS: dict[str, int] = {"lease": 35, "occupants": 20, "moveIn": 15, "deposit": 15, "rent": 10, "early": 5}
CLOSE_CALL_POINTS = 5.0
MAX_REQUESTS = 10


def _number(value: Any) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _moment(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _money(value: Decimal | None) -> str:
    return "chưa rõ" if value is None else f"{int(value):,}".replace(",", ".") + " đ"


def _date_text(value: date | None) -> str:
    return "chưa rõ" if value is None else value.strftime("%d/%m/%Y")


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    lease_months: Decimal | None
    occupants: Decimal | None
    move_in: date | None
    deposit: Decimal | None
    monthly_rent: Decimal | None
    lease_total: Decimal | None
    created_at: datetime | None
    note: str | None

    @staticmethod
    def parse(item: dict[str, Any]) -> "Candidate":
        rent = _number(item.get("effectiveMonthlyRent"))
        if rent is None:
            rent = _number(item.get("monthlyRentPrice"))
        note = item.get("renterNote")
        return Candidate(
            id=str(item.get("id")), name=str(item.get("renterName") or "Người thuê").strip()[:80],
            lease_months=_number(item.get("leaseMonths")), occupants=_number(item.get("occupantCount")),
            move_in=_day(item.get("moveInDate")), deposit=_number(item.get("depositAmount")),
            monthly_rent=rent, lease_total=_number(item.get("estimatedLeaseTotal")),
            created_at=_moment(item.get("createdAt")),
            note=str(note).strip()[:300] if isinstance(note, str) and note.strip() else None)


@dataclass(frozen=True)
class Criterion:
    key: str
    value: Callable[[Candidate], Any]
    higher_is_better: bool
    best: Callable[[Candidate], str]   # phrase when this request is the best
    worst: Callable[[Candidate], str]  # phrase when it is the worst


def _ordinal(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return float(value.toordinal())
    return float(value)


CRITERIA: tuple[Criterion, ...] = (
    Criterion("lease", lambda c: c.lease_months, True,
              lambda c: f"thuê dài nhất ({int(c.lease_months)} tháng) nên ổn định doanh thu hơn",
              lambda c: f"chỉ thuê {int(c.lease_months)} tháng, ngắn nhất nên dễ trống phòng sớm"),
    Criterion("occupants", lambda c: c.occupants, False,
              lambda c: f"ở ít người nhất ({int(c.occupants)} người) nên ít hao mòn phòng và điện nước hơn",
              lambda c: f"ở nhiều người nhất ({int(c.occupants)} người) nên hao mòn và chi phí vận hành cao hơn"),
    Criterion("moveIn", lambda c: c.move_in, False,
              lambda c: f"dọn vào sớm nhất ({_date_text(c.move_in)}) nên rút ngắn thời gian trống phòng",
              lambda c: f"dọn vào muộn nhất ({_date_text(c.move_in)}) nên phòng trống lâu hơn"),
    Criterion("deposit", lambda c: c.deposit, True,
              lambda c: f"đặt cọc cao nhất ({_money(c.deposit)}) nên bảo đảm tốt hơn",
              lambda c: f"đặt cọc thấp nhất ({_money(c.deposit)})"),
    Criterion("rent", lambda c: c.monthly_rent, True,
              lambda c: f"tiền thuê mỗi tháng cao nhất ({_money(c.monthly_rent)})",
              lambda c: f"tiền thuê mỗi tháng thấp nhất ({_money(c.monthly_rent)})"),
    Criterion("early", lambda c: c.created_at, False,
              lambda c: "gửi yêu cầu sớm nhất",
              lambda c: "gửi yêu cầu muộn nhất"),
)

LABELS = {"lease": "thời hạn thuê", "occupants": "số người ở", "moveIn": "ngày dọn vào",
          "deposit": "tiền cọc", "rent": "tiền thuê tháng", "early": "thời điểm gửi yêu cầu"}


def _scores(candidates: list[Candidate], criterion: Criterion) -> dict[str, float] | None:
    """0..1 per candidate, or None when the criterion cannot tell the requests apart."""
    values = {c.id: _ordinal(criterion.value(c)) for c in candidates}
    known = [v for v in values.values() if v is not None]
    if not known:
        return None
    if max(known) == min(known):
        if len(known) == len(values):
            return None
        return {key: 0.0 if value is None else 1.0 for key, value in values.items()}  # only some have the data
    low, high = min(known), max(known)
    result = {}
    for key, value in values.items():
        if value is None:
            result[key] = 0.0  # missing data never earns points
            continue
        fraction = (value - low) / (high - low)
        result[key] = fraction if criterion.higher_is_better else 1.0 - fraction
    return result


def review(listing: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Rank 2..n PENDING requests of one listing. Raises ValueError for fewer than two."""
    candidates = [Candidate.parse(item) for item in items[:MAX_REQUESTS]]
    if len(candidates) < 2:
        raise ValueError("At least two requests are required")
    by_criterion = {c.key: _scores(candidates, c) for c in CRITERIA}
    applicable = {key: scores for key, scores in by_criterion.items() if scores is not None}
    total_weight = sum(WEIGHTS[key] for key in applicable) or 1

    rows = []
    for candidate in candidates:
        points = sum(WEIGHTS[key] * scores[candidate.id] for key, scores in applicable.items())
        strengths, weaknesses, breakdown = [], [], {}
        for criterion in CRITERIA:
            scores = applicable.get(criterion.key)
            if scores is None:
                continue
            breakdown[criterion.key] = round(scores[candidate.id] * 100)
            if criterion.value(candidate) is None:
                continue
            if scores[candidate.id] == 1.0:
                strengths.append(criterion.best(candidate))
            elif scores[candidate.id] == 0.0:
                weaknesses.append(criterion.worst(candidate))
        rows.append({
            "requestId": candidate.id, "renterName": candidate.name,
            "score": round(points * 100 / total_weight, 1), "breakdown": breakdown,
            "facts": {"leaseMonths": None if candidate.lease_months is None else int(candidate.lease_months),
                      "occupantCount": None if candidate.occupants is None else int(candidate.occupants),
                      "moveInDate": candidate.move_in.isoformat() if candidate.move_in else None,
                      "depositAmount": None if candidate.deposit is None else int(candidate.deposit),
                      "monthlyRent": None if candidate.monthly_rent is None else int(candidate.monthly_rent),
                      "estimatedLeaseTotal": None if candidate.lease_total is None else int(candidate.lease_total),
                      "createdAt": candidate.created_at.isoformat() if candidate.created_at else None},
            "renterNote": candidate.note, "strengths": strengths, "weaknesses": weaknesses,
        })
    # Ties: the earlier request wins (first come, first served) - already part of the score, so stable order is enough.
    rows.sort(key=lambda row: -row["score"])
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    margin = round(rows[0]["score"] - rows[1]["score"], 1)
    result = {
        "listingId": listing.get("listingId"), "listingTitle": listing.get("listingTitle"),
        "count": len(rows), "weights": {key: WEIGHTS[key] for key in applicable},
        "recommendedRequestId": rows[0]["requestId"], "margin": margin,
        "closeCall": margin < CLOSE_CALL_POINTS, "ranking": rows,
        "decisiveCriteria": [LABELS[key] for key in sorted(applicable, key=lambda k: -WEIGHTS[k])],
    }
    result["summary"] = summary(result)
    return result


def summary(result: dict[str, Any]) -> str:
    top, runner = result["ranking"][0], result["ranking"][1]
    lines = [f"Mình đề xuất chọn yêu cầu của **{top['renterName']}** ({top['score']:g}/100)."]
    if top["strengths"]:
        lines.append("Lý do: " + "; ".join(top["strengths"][:3]) + ".")
    elif result["closeCall"]:
        lines.append("Các yêu cầu hiện không khác nhau đáng kể ở các tiêu chí đang xét.")
    if runner["strengths"]:
        lines.append(f"**{runner['renterName']}** ({runner['score']:g}/100) có điểm mạnh: "
                     + "; ".join(runner["strengths"][:2]) + ".")
    if len(result["ranking"]) > 2:
        others = ", ".join(f"{row['renterName']} ({row['score']:g})" for row in result["ranking"][2:])
        lines.append(f"Các yêu cầu còn lại: {others}.")
    if result["closeCall"]:
        lines.append(f"Chênh lệch chỉ {result['margin']:g} điểm nên đây là lựa chọn sát nhau; bạn nên cân nhắc thêm "
                     "các yếu tố mình chưa chấm (ví dụ trao đổi trực tiếp với khách).")
    lines.append("Mình chấm theo: " + ", ".join(
        f"{LABELS_BY_KEY[key]} {weight}%" for key, weight in _percent(result["weights"]).items()) + ".")
    lines.append("Đây chỉ là gợi ý hỗ trợ, quyền quyết định vẫn thuộc về bạn.")
    return "\n\n".join(lines)


LABELS_BY_KEY = LABELS


def _percent(weights: dict[str, int]) -> dict[str, int]:
    total = sum(weights.values()) or 1
    return {key: round(value * 100 / total) for key, value in weights.items()}
