"""Renter search state v1, request dates, catalog typing and revision persistence (T07, T08, T23, T27, T31)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.listing_catalog import CatalogError, SearchCatalog, money
from homespace_ai.application.renter_dates import request_today, resolve
from homespace_ai.core import business_properties
from homespace_ai.repositories.conversation_repo import ConversationRepository, conversation_search_state
from tests.unit.listing_fakes import CATALOG, IDS

CAT = SearchCatalog(CATALOG)
PLACE = "11111111-1111-1111-1111-111111111111"


def hard(field, op, value):
    return {"field": field, "op": op, "value": value, "strength": "hard"}


def test_omitted_criteria_are_kept_and_same_bound_supersedes():
    state = rs.merge(None, {"constraints": [hard("amenity.ELEVATOR", "eq", True), hard("rent.monthly_amount", "lte", "3000000"),
                                            hard("rent.monthly_amount", "gte", "2000000")]}, "")
    state = rs.merge(state, {"constraints": [hard("rent.monthly_amount", "lte", "5000000")]}, "tăng ngân sách")
    found = {(c["field"], c["op"], c["value"]) for c in state["constraints"]}
    assert found == {("amenity.ELEVATOR", "eq", True), ("rent.monthly_amount", "lte", "5000000"), ("rent.monthly_amount", "gte", "2000000")}
    assert state["turn"] == 2


def test_contains_is_keyed_by_value_and_removal_needs_remove_fields():
    state = rs.merge(None, {"constraints": [hard("display", "contains", "chợ"), hard("display", "contains", "hẻm xe hơi")]}, "")
    assert len(state["constraints"]) == 2
    state = rs.merge(state, {"constraints": []}, "ok")
    assert len(state["constraints"]) == 2
    state = rs.merge(state, {"removeFields": ["display", "context.occupants"]}, "bỏ")
    assert state["constraints"] == []


def test_reset_requires_customer_words_and_keeps_paging_ids_only():
    state = rs.merge(None, {"constraints": [hard("amenity.ELEVATOR", "eq", True)], "location": {"wardId": "24076"}}, "")
    state = rs.record_shown(state, IDS[:2], more=False)
    assert rs.merge(state, {}, "Tìm thêm")["constraints"]
    reset = rs.merge(state, {}, "Tìm lại từ đầu giúp mình")
    assert reset["constraints"] == [] and reset["location"] is None and reset["shown"]["ids"] == IDS[:2]


def test_conflicts_are_detected_not_swapped():
    state = rs.merge(None, {"constraints": [hard("rent.monthly_amount", "gte", "5000000"), hard("rent.monthly_amount", "lte", "4000000")]}, "")
    conflict = rs.conflicts(state)
    assert conflict and conflict[0]["lower"]["value"] == "5000000" and conflict[0]["upper"]["value"] == "4000000"
    equal = rs.merge(None, {"constraints": [hard("area_m2", "gt", "20"), hard("area_m2", "lte", "20")]}, "")
    assert rs.conflicts(equal)
    assert not rs.conflicts(rs.merge(None, {"constraints": [hard("area_m2", "gte", "20"), hard("area_m2", "lte", "20")]}, ""))


def test_category_incompatibility_lists_scoped_fields():
    state = rs.merge(None, {"constraints": [hard("room.balcony_type", "eq", "PRIVATE"), hard("category", "eq", "APARTMENT")]}, "")
    assert [c["field"] for c in rs.incompatible(state)] == ["room.balcony_type"]
    assert rs.incompatible(rs.without(state, rs.incompatible(state))) == []


def test_preferences_never_reach_core_and_booleans_are_strings():
    state = rs.merge(None, {"constraints": [hard("amenity.ELEVATOR", "eq", True),
                                            {"field": "rent.monthly_amount", "op": "approx", "value": "3000000", "strength": "preference"}],
                            "location": {"wardId": "24076", "cityId": "12"}}, "")
    payload = rs.core_request(state, limit=5)
    assert payload["where"]["all"] == [{"field": "amenity.ELEVATOR", "op": "eq", "value": "true"},
                                       {"field": "ward_id", "op": "eq", "value": "24076"}, {"field": "city_id", "op": "eq", "value": "12"}]
    assert payload["filters"] == [] and payload["sort"] == "newest" and payload["evidence"] is True


def test_alternatives_become_one_or_group_and_target_sort_needs_target():
    state = rs.merge(None, {"alternatives": [[hard("category", "eq", "ROOM"), hard("room.restroom_type", "eq", "PRIVATE")],
                                             [hard("category", "eq", "APARTMENT")]], "sort": "target_distance"}, "")
    payload = rs.core_request(state, limit=5)
    assert payload["where"]["all"] == [{"any": [{"all": [{"field": "category", "op": "eq", "value": "ROOM"},
                                                          {"field": "room.restroom_type", "op": "eq", "value": "PRIVATE"}]},
                                                 {"all": [{"field": "category", "op": "eq", "value": "APARTMENT"}]}]}]
    assert payload["sort"] == "newest"


def test_t27_paging_is_bound_to_the_criteria_fingerprint():
    state = rs.merge(None, {"location": {"wardId": "24076"}}, "")
    state = rs.record_shown(state, IDS[:3], more=False)
    state = rs.record_shown(state, IDS[3:6], more=True)
    assert state["shown"]["ids"] == IDS[:6]
    changed = rs.merge(state, {"constraints": [hard("amenity.ELEVATOR", "eq", True)]}, "")
    assert rs.fingerprint(changed) != state["shown"]["fingerprint"]
    assert rs.record_shown(changed, IDS[6:7], more=True)["shown"]["ids"] == IDS[6:7]


def test_client_supplied_state_is_sanitized_and_cannot_inject_ids_or_access():
    raw = {"version": 1, "revision": -5, "constraints": [hard("x" * 500, "eq", "a"), hard("amenity.ELEVATOR", "drop table", "1"),
                                                          hard("amenity.ELEVATOR", "eq", {"$gt": ""})],
           "target": {"placeId": "not-a-uuid", "radiusMeters": 999999}, "shown": {"ids": ["../../admin", IDS[0]]},
           "ownerId": "someone-else", "listingIds": ["x"], "pending": {"type": "ADMIN"}}
    state = rs.sanitize(raw)
    assert state["constraints"] == [] and state["revision"] == 0 and state["target"] is None
    assert state["shown"]["ids"] == [IDS[0]] and state["pending"] is None
    assert "ownerId" not in state and "listingIds" not in state
    assert rs.sanitize({"version": 99}) is None


def test_t07_latest_state_reads_the_full_transcript_not_the_prompt_window():
    state = rs.merge(None, {"constraints": [hard("amenity.ELEVATOR", "eq", True)]}, "")
    messages = [{"role": "assistant", "listingContext": rs.listing_context([IDS[0]], state)}]
    messages += [{"role": "user", "content": f"câu {n}"} for n in range(30)]
    assert rs.latest_state(messages)["constraints"][0]["field"] == "amenity.ELEVATOR"
    assert rs.latest_state([{"role": "user", "listingContext": rs.listing_context([], state)}]) is None


def test_t08_conversation_level_state_wins_and_legacy_v1_has_none():
    newer = rs.merge(None, {"constraints": [hard("amenity.GYM", "eq", True)]}, "")
    older = rs.merge(None, {"constraints": [hard("amenity.ELEVATOR", "eq", True)]}, "")
    conversation = {"renterSearchState": newer, "messages": [{"role": "assistant", "listingContext": rs.listing_context([], older)}]}
    assert conversation_search_state(conversation)["constraints"][0]["field"] == "amenity.GYM"
    assert conversation_search_state({"messages": [{"role": "assistant", "listingContext": {"version": 1, "listingIds": [IDS[0]]}}]}) is None


@pytest.mark.asyncio
async def test_t08_revision_guard_never_lets_a_slower_request_overwrite_newer_criteria():
    repo = ConversationRepository.__new__(ConversationRepository)
    repo.collection = SimpleNamespace(update_one=AsyncMock(side_effect=[SimpleNamespace(matched_count=0), SimpleNamespace(matched_count=1)]))
    state = {**rs.empty_state(), "revision": 3}
    await repo.append_message("owner", "conv", role="assistant", content="x", status="ANSWERED",
                              listing_context=rs.listing_context([IDS[0]], state))
    guarded, fallback = repo.collection.update_one.call_args_list
    assert guarded.args[0]["ownerId"] == "owner" and {"renterSearchState.revision": {"$lt": 3}} in guarded.args[0]["$or"]
    assert guarded.args[1]["$set"]["renterSearchState"]["revision"] == 3
    pushed = fallback.args[1]["$push"]["messages"]
    assert "renterSearchState" not in fallback.args[1]["$set"]
    assert pushed["listingContext"]["stateConflict"] is True and "searchState" not in pushed["listingContext"]


def test_t23_dates_use_a_fixed_aware_clock_and_configured_offset():
    late_utc = datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc)
    assert request_today(lambda: late_utc, timedelta(hours=7)) == date(2026, 11, 1)
    with pytest.raises(ValueError):
        request_today(lambda: datetime(2026, 10, 31), timedelta(hours=7))
    today = date(2026, 12, 20)
    assert resolve("vào ở đầu tháng sau", today).move_in_by == date(2027, 1, 1)
    assert resolve("cuối tháng sau", date(2026, 1, 10)).move_in_by == date(2026, 2, 28)
    assert resolve("dọn vào ngay", today).move_in_by == today
    assert resolve("vào ngày 05/01", today).move_in_by == date(2027, 1, 5)
    assert resolve("tháng sau mình chuyển vào", today).ambiguous == "DATE"
    assert resolve("thuê 6 tháng", today).lease_months == 6
    assert resolve("vào ngày 31/02/2027", today).ambiguous == "DATE"


@pytest.mark.parametrize("content", ["", "listing.search-utc-offset=", "listing.search-utc-offset=7", "listing.search-utc-offset=+7:00",
                                     "listing.search-utc-offset=+15:00", "listing.search-utc-offset=Asia/Ho_Chi_Minh"])
def test_offset_property_is_required_and_strict(tmp_path, monkeypatch, content):
    path = tmp_path / "application.properties"
    path.write_text(content + "\n", encoding="utf-8")
    monkeypatch.setattr(business_properties, "PROPERTIES_FILE", path)
    with pytest.raises(ValueError, match="listing.search-utc-offset"):
        business_properties.listing_search_utc_offset()


def test_offset_property_parses_sign(tmp_path, monkeypatch):
    path = tmp_path / "application.properties"
    path.write_text("listing.search-utc-offset=-03:30\n", encoding="utf-8")
    monkeypatch.setattr(business_properties, "PROPERTIES_FILE", path)
    assert business_properties.listing_search_utc_offset() == -timedelta(hours=3, minutes=30)


@pytest.mark.parametrize("text,amount", [("5tr", 5000000), ("3 triệu", 3000000), ("2 củ", 2000000), ("800k", 800000),
                                         ("3.500.000", 3500000), ("2,5tr", 2500000)])
def test_money_words(text, amount):
    assert money(text) == Decimal(amount)


def test_catalog_typing_and_rejections():
    assert CAT.validate(hard("room.restroom_type", "eq", "private"))["value"] == "PRIVATE"
    assert CAT.validate(hard("amenity.ELEVATOR", "eq", "true"))["value"] is True
    assert CAT.validate(hard("rent.monthly_amount", "lte", "5tr"))["value"] == "5000000"
    assert CAT.validate({"field": "policy.petPolicy.value", "op": "in", "value": ["ALLOWED", "APPROVAL_REQUIRED"], "strength": "hard"})["value"] == ["ALLOWED", "APPROVAL_REQUIRED"]
    for bad in [hard("display.highlights", "contains", "x"), hard("amenity.ELEVATOR", "gt", "true"),
                hard("rent.monthly_amount", "approx", "3000000"), hard("room.restroom_type", "eq", "PRIVATE_ONLY"),
                hard("available_from", "lte", "next month")]:
        with pytest.raises(CatalogError):
            CAT.validate(bad)
    assert "display.highlights" not in CAT.searchable()
