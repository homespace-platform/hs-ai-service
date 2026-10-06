"""Bounded, read-only access to published listings in the core PostgreSQL database.

The LLM never supplies SQL. It may only fill validated filter parameters.
"""

from functools import lru_cache
import unicodedata

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from homespace_ai.core.config import get_settings


class ListingFilters(BaseModel):
    province_code: str = Field(min_length=1, max_length=4)
    district: str | None = Field(default=None, max_length=100)
    category: str | None = None
    price_max: int | None = Field(default=None, ge=0, le=1_000_000_000)
    has_mezzanine: bool = False
    has_balcony: bool = False
    has_parking: bool = False
    has_garage: bool | None = None
    has_video: bool = False
    location: str | None = Field(default=None, max_length=120)
    landmark: str | None = Field(default=None, max_length=120)
    listing_ids: list[str] | None = Field(default=None, max_length=20)
    page: int = Field(default=1, ge=1, le=100)
    size: int = Field(default=10, ge=1, le=20)
    sort: str = "newest"


@lru_cache
def listing_engine():
    return create_async_engine(
        get_settings().listing_database_url, pool_pre_ping=True, pool_size=3, max_overflow=2
    )


def _contains(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    # ILIKE parameters stay values; escaping prevents wildcard input from broadening a filter.
    escaped = value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_BASE = """
FROM listings l
JOIN addresses a ON a.listing_id = l.id AND a.active IS TRUE
LEFT JOIN listing_room_details r ON r.listing_id = l.id
LEFT JOIN listing_apartment_details ap ON ap.listing_id = l.id
LEFT JOIN listing_house_details h ON h.listing_id = l.id
WHERE l.active IS TRUE AND l.status = 'PUBLISHED'
  AND (l.expires_at IS NULL OR l.expires_at > now())
  AND a.province_code = :province_code
  AND (CAST(:district AS text) IS NULL OR a.full_address ILIKE :district ESCAPE '\\')
  AND (CAST(:category AS text) IS NULL OR l.category = :category)
  AND (CAST(:price_max AS numeric) IS NULL OR l.price_amount <= :price_max)
  AND (:has_mezzanine IS FALSE OR (l.category = 'ROOM' AND r.has_mezzanine IS TRUE))
  AND (:has_balcony IS FALSE OR
       (l.category = 'ROOM' AND r.has_balcony IS TRUE) OR
       (l.category = 'APARTMENT' AND ap.balcony_direction IS NOT NULL))
  AND (:has_parking IS FALSE OR
       (l.category = 'ROOM' AND r.parking_policy IN ('PAID', 'FREE') AND COALESCE(r.max_vehicles, 0) > 0) OR
       (l.category = 'HOUSE' AND h.has_garage IS TRUE AND COALESCE(h.max_vehicles, 0) > 0) OR
       (l.category = 'APARTMENT' AND COALESCE(l.max_motorbike_count, 0) > 0))
  AND (CAST(:has_garage AS boolean) IS NULL OR
       (l.category = 'HOUSE' AND h.has_garage = CAST(:has_garage AS boolean)))
  AND (:has_video IS FALSE OR EXISTS (
       SELECT 1 FROM listing_media m WHERE m.listing_id=l.id
         AND m.active IS TRUE AND m.media_type='VIDEO'))
  AND (CAST(:location AS text) IS NULL OR a.full_address ILIKE :location ESCAPE '\\')
  AND (CAST(:landmark AS text) IS NULL OR
       l.title ILIKE :landmark ESCAPE '\\' OR
       l.description ILIKE :landmark ESCAPE '\\' OR
       a.full_address ILIKE :landmark ESCAPE '\\' OR
       ap.project_name ILIKE :landmark ESCAPE '\\')
  AND (CAST(:listing_ids AS text[]) IS NULL OR l.id = ANY(CAST(:listing_ids AS text[])))
"""


async def search_public_listings(filters: ListingFilters) -> dict:
    """Return IDs in a stable order; the client hydrates via the existing public detail API."""
    values = {
        "province_code": filters.province_code.zfill(2),
        "district": _contains(filters.district),
        "category": filters.category if filters.category in {"ROOM", "APARTMENT", "HOUSE"} else None,
        "price_max": filters.price_max,
        "has_mezzanine": filters.has_mezzanine,
        "has_balcony": filters.has_balcony,
        "has_parking": filters.has_parking,
        "has_garage": filters.has_garage,
        "has_video": filters.has_video,
        "location": _contains(filters.location),
        # A landmark is a hard match only when the query did not also give a locality.
        # Without coordinates, a 'near X' claim cannot be inferred from another ward.
        "landmark": _contains(filters.landmark) if not (filters.location or filters.district) else None,
        "listing_ids": filters.listing_ids or None,
        "limit": filters.size,
        "offset": (filters.page - 1) * filters.size,
    }
    order_by = {
        "price_asc": "l.price_amount ASC, l.id",
        "price_desc": "l.price_amount DESC, l.id",
        "area_desc": "l.area_m2 DESC, l.id",
    }.get(filters.sort, "l.published_at DESC NULLS LAST, l.id")
    async with listing_engine().connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            count = (await conn.execute(text("SELECT count(*) " + _BASE), values)).scalar_one()
            rows = (await conn.execute(text(
                "SELECT l.id, l.title, l.category, l.price_amount, l.price_unit, "
                "l.area_m2, a.ward_name, "
                "r.parking_policy, r.max_vehicles AS room_max_vehicles, "
                "h.has_garage, h.max_vehicles AS house_max_vehicles, "
                "l.max_motorbike_count " + _BASE +
                f" ORDER BY {order_by} LIMIT :limit OFFSET :offset"
            ), values)).mappings().all()
    return {
        "total": count,
        "page": filters.page,
        "size": filters.size,
        "listing_ids": [row["id"] for row in rows],
        "matches": [
            {
                "id": row["id"], "title": row["title"], "category": row["category"],
                "price": int(row["price_amount"]), "priceUnit": row["price_unit"],
                "areaM2": float(row["area_m2"]) if row["area_m2"] is not None else None,
                "ward": row["ward_name"],
                "parkingPolicy": row["parking_policy"],
                "maxVehicles": row["room_max_vehicles"] if row["category"] == "ROOM" else row["house_max_vehicles"],
                "hasGarage": row["has_garage"], "maxMotorbikeCount": row["max_motorbike_count"],
            }
            for row in rows
        ],
    }


_PLACE_SUGGESTIONS: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {
    # (landmark, locality shown to the renter, district/ward aliases)
    "79": [
        ("Đại học Công nghiệp TP.HCM (IUH)", "Gò Vấp", ("go vap", "hanh thong")),
        ("Chợ Hạnh Thông Tây", "Gò Vấp", ("go vap", "an hoi dong")),
        ("Công viên Gia Định", "Gò Vấp - Phú Nhuận", ("go vap", "phu nhuan")),
        ("Đại học Bách khoa TP.HCM", "Quận 10", ("quan 10",)),
        ("Chợ Bến Thành", "Quận 1", ("quan 1",)),
        ("Landmark 81", "Bình Thạnh", ("binh thanh",)),
        ("Crescent Mall", "Quận 7", ("quan 7",)),
        ("AEON Mall Tân Phú", "Tân Phú", ("tan phu",)),
        ("Suối Tiên", "TP. Thủ Đức", ("thu duc",)),
    ],
    "01": [
        ("Lăng Chủ tịch Hồ Chí Minh", "Ba Đình", ("ba dinh",)),
        ("Lotte Center Hanoi", "Ba Đình", ("ba dinh",)),
        ("Hồ Tây", "Tây Hồ", ("tay ho",)),
        ("Văn Miếu - Quốc Tử Giám", "Đống Đa", ("dong da",)),
        ("Đại học Bách khoa Hà Nội", "Hai Bà Trưng", ("hai ba trung",)),
    ],
    "48": [
        ("Cầu Rồng", "Hải Châu", ("hai chau",)),
        ("Công viên APEC", "Hải Châu", ("hai chau",)),
        ("Đại học Đà Nẵng", "Hải Châu", ("hai chau",)),
        ("Bãi biển Mỹ Khê", "Sơn Trà - Ngũ Hành Sơn", ("son tra", "ngu hanh son")),
    ],
    "75": [
        ("Khu du lịch Bửu Long", "Biên Hòa", ("bien hoa",)),
        ("Đại học Lạc Hồng", "Biên Hòa", ("bien hoa",)),
        ("Vincom Plaza Biên Hòa", "Biên Hòa", ("bien hoa",)),
        ("Khu công nghiệp Amata", "Biên Hòa", ("bien hoa",)),
    ],
}


async def suggest_places(province_code: str, district: str | None = None) -> list[dict[str, str]]:
    """Suggest well-known destinations, never ward or street names from seeded listing data."""
    places = _PLACE_SUGGESTIONS.get(province_code.zfill(2), [])
    if district:
        normalized_district = _plain(district)
        places = [
            place for place in places
            if any(alias in normalized_district or normalized_district in alias
                   for alias in place[2])
        ]
    return [
        {
            "label": f"{name} · {locality}",
            "searchText": f"Tìm chỗ ở gần {name}",
        }
        for name, locality, _aliases in places[:8]
    ]


def _plain(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    return "".join(char for char in value if unicodedata.category(char) != "Mn")


async def match_listing_ward(province_code: str, query: str) -> str | None:
    """Resolve a named ward in the user's text against live published addresses."""
    statement = text("""
        SELECT DISTINCT a.ward_name FROM listings l
        JOIN addresses a ON a.listing_id=l.id AND a.active IS TRUE
        WHERE l.active IS TRUE AND l.status='PUBLISHED'
          AND (l.expires_at IS NULL OR l.expires_at > now())
          AND a.province_code=:province_code
    """)
    async with listing_engine().connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            wards = (await conn.execute(statement, {"province_code": province_code.zfill(2)})).scalars().all()
    normalized_query = _plain(query)
    matches = []
    for ward in wards:
        short_name = _plain(ward).removeprefix("phuong ").removeprefix("xa ")
        if len(short_name) >= 3 and short_name in normalized_query:
            matches.append((len(short_name), ward))
    return max(matches)[1] if matches else None
