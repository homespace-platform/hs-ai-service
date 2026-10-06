"""Extract a bounded listing intent; never allow model-generated SQL or arbitrary filters."""

import json
import re
import unicodedata

from pydantic import BaseModel, Field, ValidationError

from homespace_ai.clients.generative import GenerationUnavailableError, get_generative_client
from homespace_ai.core.config import get_settings


class SearchIntent(BaseModel):
    category: str | None = None
    price_max: int | None = Field(default=None, ge=0, le=1_000_000_000)
    has_mezzanine: bool = False
    has_balcony: bool = False
    has_parking: bool = False
    location: str | None = Field(default=None, max_length=120)
    landmark: str | None = Field(default=None, max_length=120)


def _plain(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _removes_criterion(query: str, aliases: tuple[str, ...]) -> bool:
    plain = _plain(query)
    for alias in aliases:
        if re.search(
            rf"(?:bo|loai bo|khong can|khong yeu cau|khong bat buoc).{{0,35}}{re.escape(alias)}",
            plain,
        ):
            return True
        if re.search(rf"{re.escape(alias)}.{{0,20}}(?:khong can|khong bat buoc|khong quan trong)", plain):
            return True
    return False


def fallback_intent(query: str) -> SearchIntent:
    plain = _plain(query)
    category = None
    if any(term in plain for term in ("phong tro", "nha tro", "tim tro", "o tro")):
        category = "ROOM"
    elif any(term in plain for term in ("can ho", "chung cu", "studio")):
        category = "APARTMENT"
    elif "nha nguyen can" in plain:
        category = "HOUSE"
    price_max = None
    price = re.search(r"(?:duoi|khong qua|toi da|tam|<=?)\s*(\d+(?:[.,]\d+)?)\s*(trieu|tr|ty|nghin|k|dong)?", plain)
    if price:
        amount = float(price.group(1).replace(",", "."))
        unit = price.group(2) or "trieu"
        multiplier = 1_000_000_000 if unit == "ty" else (1_000_000 if unit in {"trieu", "tr"} else (1_000 if unit in {"nghin", "k"} else 1))
        price_max = int(amount * multiplier)
    location = None
    if "go vap" in plain:
        location = "Gò Vấp"
    elif match := re.search(
        r"\b(?:tại|ở|tai|o)\s+([^,.]+?)(?:\s+(?:dưới|giá|có|cần|duoi|gia|co|can)|$)",
        query,
        re.IGNORECASE,
    ):
        # Keep the user's original accents: PostgreSQL ILIKE is not accent-insensitive.
        location = match.group(1).strip() or None
    landmark = None
    if match := re.search(r"\bgan\s+([^,.]+?)(?:\s+(?:toi|minh|gia|duoi|co|can)|$)", plain):
        landmark = match.group(1).strip() or None
    return SearchIntent(
        category=category,
        price_max=price_max,
        has_mezzanine=(
            bool(re.search(r"\b(co gac|gac lung|gac xep)\b", plain))
            and not _removes_criterion(query, ("gac", "gac lung", "gac xep"))
        ),
        has_balcony=("ban cong" in plain and not _removes_criterion(query, ("ban cong",))),
        has_parking=(
            any(term in plain for term in ("gui xe", "cho de xe", "bai do xe", "do xe"))
            and not _removes_criterion(query, ("gui xe", "cho de xe", "bai do xe", "do xe"))
            and not any(term in plain for term in (
                "khong can gui xe", "khong gui xe", "khong co cho gui xe", "khong co cho de xe"
            ))
        ),
        location=location,
        landmark=landmark,
    )


def _merge_history_intent(query: str, previous_user_messages: list[str]) -> SearchIntent:
    state = SearchIntent()
    for message in [*previous_user_messages, query]:
        update = fallback_intent(message)
        state = SearchIntent(
            category=update.category or state.category,
            price_max=(None if _removes_criterion(message, ("gia", "ngan sach"))
                       else update.price_max if update.price_max is not None else state.price_max),
            has_mezzanine=(False if _removes_criterion(message, ("gac", "gac lung", "gac xep"))
                           else update.has_mezzanine or state.has_mezzanine),
            has_balcony=(False if _removes_criterion(message, ("ban cong",))
                         else update.has_balcony or state.has_balcony),
            has_parking=(False if _removes_criterion(message, ("gui xe", "cho de xe", "bai do xe", "do xe"))
                         else update.has_parking or state.has_parking),
            location=update.location or state.location,
            landmark=update.landmark or state.landmark,
        )
    return state


async def parse_intent(query: str, previous_user_messages: list[str] | None = None) -> SearchIntent:
    previous_user_messages = (previous_user_messages or [])[-8:]
    fallback = _merge_history_intent(query, previous_user_messages)
    settings = get_settings()
    if settings.generation_provider.lower().strip() not in {"groq", "gemini"}:
        return fallback
    try:
        prompt = query
        if previous_user_messages:
            history = "\n".join(f"Người dùng: {item[:1000]}" for item in previous_user_messages)
            prompt = f"LỊCH SỬ YÊU CẦU TÌM NHÀ:\n{history}\n\nTIN NHẮN HIỆN TẠI CẦN CẬP NHẬT BỘ LỌC:\n{query}"
        answer = await get_generative_client(settings).generate_answer(prompt, [], mode="property_search")
        cleaned = answer.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            return fallback
        intent = SearchIntent.model_validate(data)
        if intent.category not in {None, "ROOM", "APARTMENT", "HOUSE"}:
            intent.category = fallback.category
        # Keep deterministic extraction as a safety net when the model omits a prior
        # filter from a short follow-up such as “có chỗ gửi xe riêng không?”.
        return SearchIntent(
            category=intent.category or fallback.category,
            price_max=(None if _removes_criterion(query, ("gia", "ngan sach"))
                       else intent.price_max if intent.price_max is not None else fallback.price_max),
            has_mezzanine=(
                False if _removes_criterion(query, ("gac", "gac lung", "gac xep"))
                else intent.has_mezzanine or fallback.has_mezzanine
            ),
            has_balcony=(
                False if _removes_criterion(query, ("ban cong",))
                else intent.has_balcony or fallback.has_balcony
            ),
            has_parking=(
                False if _removes_criterion(query, ("gui xe", "cho de xe", "bai do xe", "do xe"))
                else intent.has_parking or fallback.has_parking
            ),
            location=intent.location or fallback.location,
            landmark=intent.landmark or fallback.landmark,
        )
    except (GenerationUnavailableError, ValidationError, ValueError, TypeError):
        return fallback
