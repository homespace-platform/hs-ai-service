"""Move-in dates and lease length from Vietnamese phrases, at request time in the configured offset."""

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from homespace_ai.application.vietnamese_text import fold_text
from homespace_ai.core.business_properties import listing_search_utc_offset

Clock = Callable[[], datetime]


def request_today(clock: Clock | None = None, offset: timedelta | None = None) -> date:
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        raise ValueError("The request clock must be timezone-aware")
    return now.astimezone(timezone(offset if offset is not None else listing_search_utc_offset())).date()


@dataclass(frozen=True)
class DateNeeds:
    move_in_by: date | None = None
    lease_months: int | None = None
    ambiguous: str | None = None


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _next_month(today: date) -> tuple[int, int]:
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


def resolve(question: str, today: date) -> DateNeeds:
    text = fold_text(question)
    move_in: date | None = None
    ambiguous: str | None = None
    year, month = _next_month(today)
    if re.search(r"\bdau thang sau\b", text):
        move_in = date(year, month, 1)
    elif re.search(r"\bgiua thang sau\b", text):
        move_in = date(year, month, 15)
    elif re.search(r"\bcuoi thang sau\b", text):
        move_in = _month_end(year, month)
    elif re.search(r"\bcuoi thang nay\b", text):
        move_in = _month_end(today.year, today.month)
    elif re.search(r"\b(?:vao o|don vao|vao) (?:ngay|lien)\b(?!\s*\d)|\bngay bay gio\b", text):
        move_in = today
    else:
        explicit = re.search(r"\bngay (\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?\b|\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))\b", text)
        if explicit:
            day, mon, yr = (explicit[1], explicit[2], explicit[3]) if explicit[1] else (explicit[4], explicit[5], explicit[6])
            try:
                candidate = date(int(yr) if yr else today.year, int(mon), int(day))
                if not yr and candidate < today:
                    candidate = date(today.year + 1, int(mon), int(day))
                move_in = candidate
            except ValueError:
                ambiguous = "DATE"
        elif re.search(r"\b(?:thang sau|tuan sau|sau tet|dau nam sau)\b", text) and re.search(r"\b(?:vao|don|o|nhan phong|chuyen)\b", text):
            ambiguous = "DATE"
    lease = re.search(r"\bthue (?:trong |khoang )?(\d{1,2}) thang\b|\bhop dong (\d{1,2}) thang\b", text)
    months = int(lease[1] or lease[2]) if lease else None
    return DateNeeds(move_in, months if months and 1 <= months <= 60 else None, ambiguous)
