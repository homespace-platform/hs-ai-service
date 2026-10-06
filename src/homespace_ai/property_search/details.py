"""Public listing facts from the same PostgreSQL rows used by listing detail pages.

Only published, active, unexpired records and public-facing columns are exposed.
The MCP caller never supplies SQL or arbitrary column names.
"""

from uuid import UUID

from sqlalchemy import text

from homespace_ai.property_search.repository import listing_engine


_COMMON = (
    "title", "description", "category", "available_from", "area_m2",
    "price_amount", "currency", "price_unit", "negotiable", "deposit_type",
    "deposit_amount", "deposit_months", "payment_cycle", "minimum_lease_months",
    "management_fee_included", "vat_included", "max_motorbike_count", "max_car_count",
)
_ADDRESS = ("province_name", "ward_name", "street_line", "full_address")
_ROOM = (
    "room_code", "floor_number", "restroom_type", "kitchen_type", "has_window",
    "balcony_type", "has_balcony", "has_mezzanine", "furnishing_status",
    "access_type", "access_hours_type", "electric_meter_type", "water_meter_type",
    "max_occupants", "max_vehicles", "parking_policy",
)
_APARTMENT = (
    "project_name", "building_block", "unit_code", "floor_number",
    "building_total_floors", "bedroom_count", "bathroom_count", "living_room_count",
    "kitchen_count", "furnishing_status", "main_door_direction", "balcony_direction",
    "view_description", "max_occupants", "legal_status",
)
_HOUSE = (
    "land_area_m2", "frontage_width_m", "length_m", "access_road_width_m",
    "frontage_count", "total_floors", "bedroom_count", "bathroom_count",
    "living_room_count", "kitchen_count", "has_rooftop", "has_garage",
    "access_type", "max_occupants", "max_vehicles", "furnishing_status",
    "legal_status", "rental_scope_description", "rented_floor_from", "rented_floor_to",
)
_CHARGE = (
    "charge_type", "billing_method", "amount", "currency", "unit",
    "included_in_rent", "custom_name", "description",
)
_FURNISHING = (
    "item_code", "asset_name", "quantity", "handover_condition", "condition_note",
)


def _owner(source: dict | None) -> dict | None:
    if not source:
        return None
    full_name = " ".join(filter(None, (source.get("first_name"), source.get("last_name")))).strip()
    return {
        "display_name": full_name or source.get("username") or "Chủ nhà",
        "phone": source.get("phone"),
        "avatar_url": source.get("avatar_url"),
    }


def _pick(source: dict | None, keys: tuple[str, ...]) -> dict | None:
    if source is None:
        return None
    return {key: source.get(key) for key in keys}


def _safe_ids(listing_ids: list[str]) -> list[str]:
    parsed: list[str] = []
    for item in listing_ids[:20]:
        try:
            parsed.append(str(UUID(item)))
        except (ValueError, TypeError):
            continue
    return list(dict.fromkeys(parsed))


async def get_public_listing_facts(listing_ids: list[str]) -> dict:
    """Fetch bounded full public facts for specific listings; never search by free text."""
    ids = _safe_ids(listing_ids)
    if not ids:
        return {"listings": []}
    params = {"ids": ids}
    listing_sql = text("""
        SELECT l.id, to_jsonb(l) AS listing, to_jsonb(a) AS address, to_jsonb(u) AS owner,
               to_jsonb(r) AS room, to_jsonb(ap) AS apartment, to_jsonb(h) AS house
        FROM listings l
        JOIN addresses a ON a.listing_id=l.id AND a.active IS TRUE
        LEFT JOIN users u ON u.id=l.owner_id
        LEFT JOIN listing_room_details r ON r.listing_id=l.id
        LEFT JOIN listing_apartment_details ap ON ap.listing_id=l.id
        LEFT JOIN listing_house_details h ON h.listing_id=l.id
        WHERE l.id=ANY(CAST(:ids AS text[])) AND l.active IS TRUE
          AND l.status='PUBLISHED' AND (l.expires_at IS NULL OR l.expires_at>now())
    """)
    async with listing_engine().connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            rows = (await conn.execute(listing_sql, params)).mappings().all()
            found = {row["id"]: {
                "id": row["id"],
                "listing": _pick(row["listing"], _COMMON),
                "address": _pick(row["address"], _ADDRESS),
                "owner": _owner(row["owner"]),
                "room": _pick(row["room"], _ROOM),
                "apartment": _pick(row["apartment"], _APARTMENT),
                "house": _pick(row["house"], _HOUSE),
                "charges": [], "amenities": [], "custom_amenities": [],
                "furnishings": [], "viewing_days": [], "viewing_slots": [], "media": [],
            } for row in rows}
            if not found:
                return {"listings": []}
            found_ids = list(found)
            query_params = {"ids": found_ids}
            charges = (await conn.execute(text("""
                SELECT listing_id, to_jsonb(c) AS data FROM listing_charges c
                WHERE listing_id=ANY(CAST(:ids AS text[])) AND active IS TRUE
                ORDER BY sort_order, id
            """), query_params)).mappings().all()
            for row in charges:
                found[row["listing_id"]]["charges"].append(_pick(row["data"], _CHARGE))

            amenities = (await conn.execute(text("""
                SELECT la.listing_id, a.code, a.name FROM listing_amenities la
                JOIN amenities a ON a.id=la.amenity_id AND a.active IS TRUE
                WHERE la.listing_id=ANY(CAST(:ids AS text[])) ORDER BY a.sort_order, a.code
            """), query_params)).mappings().all()
            for row in amenities:
                found[row["listing_id"]]["amenities"].append({"code": row["code"], "name": row["name"]})

            custom = (await conn.execute(text("""
                SELECT listing_id, name FROM listing_custom_amenities
                WHERE listing_id=ANY(CAST(:ids AS text[])) ORDER BY name
            """), query_params)).mappings().all()
            for row in custom:
                found[row["listing_id"]]["custom_amenities"].append(row["name"])

            furnishings = (await conn.execute(text("""
                SELECT listing_id, to_jsonb(f) AS data FROM listing_furnishing_assets f
                WHERE listing_id=ANY(CAST(:ids AS text[])) ORDER BY sort_order, id
            """), query_params)).mappings().all()
            for row in furnishings:
                found[row["listing_id"]]["furnishings"].append(_pick(row["data"], _FURNISHING))

            days = (await conn.execute(text("""
                SELECT listing_id, day_of_week FROM listing_viewing_days
                WHERE listing_id=ANY(CAST(:ids AS text[])) ORDER BY day_of_week
            """), query_params)).mappings().all()
            for row in days:
                found[row["listing_id"]]["viewing_days"].append(row["day_of_week"])
            slots = (await conn.execute(text("""
                SELECT listing_id, viewing_slot FROM listing_viewing_slots
                WHERE listing_id=ANY(CAST(:ids AS text[])) ORDER BY viewing_slot
            """), query_params)).mappings().all()
            for row in slots:
                found[row["listing_id"]]["viewing_slots"].append(row["viewing_slot"])

            media = (await conn.execute(text("""
                SELECT listing_id, media_type, media_url, is_cover FROM listing_media
                WHERE listing_id=ANY(CAST(:ids AS text[])) AND active IS TRUE
                ORDER BY sort_order, id
            """), query_params)).mappings().all()
            for row in media:
                found[row["listing_id"]]["media"].append({
                    "type": row["media_type"], "url": row["media_url"], "cover": row["is_cover"]
                })
    return {"listings": [found[item] for item in ids if item in found]}
