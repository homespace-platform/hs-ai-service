import json
from datetime import date

import pytest

from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.listing_catalog import CatalogError, SearchCatalog
from homespace_ai.application.renter_criteria import normalize
from homespace_ai.clients.generative import GenerationUnavailableError
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner, ToolPlan
from homespace_ai.core.config import Settings
from tests.unit.listing_fakes import CATALOG, ID, c, search

CAT = SearchCatalog(CATALOG)
TODAY = date(2026, 10, 29)


def config():
    return Settings(_env_file=None, GENERATION_PROVIDER="openai", OPENAI_API_KEY="test-only-key")


def call(name, args):
    return {"status": "completed", "output": [{"type": "function_call", "name": name,
                                             "arguments": json.dumps(args)}]}


def strict(schema, path="$"):
    if schema.get("type") == "object" or (isinstance(schema.get("type"), list) and "object" in schema["type"]):
        assert schema["additionalProperties"] is False, path
        assert set(schema["required"]) == set(schema["properties"]), path
        for key, value in schema["properties"].items():
            strict(value, f"{path}.{key}")
    if "items" in schema:
        strict(schema["items"], path + "[]")


async def plan_with(monkeypatch, response, question="Tìm phòng", history=(), state=None, settings=None):
    planner = ListingToolPlanner(settings or config())
    seen = {}

    async def fake_post(payload):
        seen["payload"] = payload
        return response(payload) if callable(response) else response

    monkeypatch.setattr(planner, "_post", fake_post)
    result = await planner.plan(question, list(history), CAT, state, TODAY)
    return result, seen.get("payload")


@pytest.mark.asyncio
async def test_strict_v2_search_schema_comes_from_core_catalog_and_state_reaches_the_model(monkeypatch):
    state = rs.merge(None, {"constraints": [{"field": "amenity.ELEVATOR", "op": "eq", "value": True, "strength": "hard"}]}, "")
    settings = config()
    settings.nearby_query_enabled = False
    result, payload = await plan_with(monkeypatch, call("search_listings", search(c("rent.monthly_amount", "lte", "5000000")).arguments),
                                      question="Tối đa 5 triệu", state=state, settings=settings)
    assert payload["store"] is False and payload["parallel_tool_calls"] is False and "reasoning" not in payload
    tools = {tool["name"]: tool for tool in payload["tools"]}
    assert set(tools) == {"search_listings", "ask_clarification", "get_listing_detail", "get_listing_details"}
    schema = tools["search_listings"]["parameters"]
    assert all(tool["strict"] is True for tool in tools.values())
    strict(schema)
    field_enum = schema["properties"]["constraints"]["items"]["properties"]["field"]["enum"]
    assert field_enum == CAT.searchable()
    assert "display.highlights" not in field_enum and "amenity.ELEVATOR" in field_enum
    assert schema["properties"]["sort"]["enum"][:-1] == CATALOG["sorts"]
    text = json.dumps(payload, ensure_ascii=False)
    assert "CURRENT_STATE" in text and "amenity.ELEVATOR" in text and "TODAY=2026-10-29" in text
    assert "policy.petPolicy.value" in text
    assert result.name == "search_listings"


@pytest.mark.asyncio
async def test_nearby_tools_are_feature_gated(monkeypatch):
    settings = config()
    settings.nearby_query_enabled = True
    _, payload = await plan_with(monkeypatch, call("get_listing_detail", {"listingId": ID}),
                                 question=f"Căn {ID} có máy lạnh không?", settings=settings)
    names = {tool["name"] for tool in payload["tools"]}
    assert "get_listing_nearby_places" in names
    kinds = payload["tools"][0]["parameters"]["properties"]["nearby"]["items"]["properties"]["kind"]["enum"]
    assert set(kinds) - {None} == {k["code"] for k in CATALOG["nearbyKinds"]}


@pytest.mark.asyncio
async def test_detail_tool_call_for_an_id_written_in_the_question(monkeypatch):
    result, _ = await plan_with(monkeypatch, call("get_listing_detail", {"listingId": ID}), question=f"Căn {ID} có máy lạnh không?")
    assert result == ToolPlan("get_listing_detail", {"listingId": ID})


@pytest.mark.asyncio
async def test_planner_cannot_invent_a_known_listing_id(monkeypatch):
    with pytest.raises(GenerationUnavailableError):
        await plan_with(monkeypatch, call("get_listing_detail", {"listingId": "00000000-0000-0000-0000-000000000009"}),
                        question="Căn này có máy lạnh không?",
                        history=[{"role": "assistant", "content": "x", "listingContext": {"version": 1, "listingIds": [ID]}}])


@pytest.mark.asyncio
@pytest.mark.parametrize("nearby_enabled", [False, True])
async def test_removed_map_tool_is_rejected(monkeypatch, nearby_enabled):
    settings = config()
    settings.nearby_query_enabled = nearby_enabled
    with pytest.raises(GenerationUnavailableError, match="Malformed OpenAI tool call"):
        await plan_with(monkeypatch, call("compare_listing_surroundings", {"listingIds": []}), settings=settings)


@pytest.mark.asyncio
async def test_legacy_near_place_tool_name_maps_to_v2_search(monkeypatch):
    result, _ = await plan_with(monkeypatch, call("find_listings_near_place", search(place="IUH").arguments))
    assert result.name == "search_listings" and result.arguments["place"]["query"] == "IUH"


@pytest.mark.asyncio
async def test_plain_text_without_question_routes_to_rag(monkeypatch):
    response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Use RAG"}]}]}
    result, _ = await plan_with(monkeypatch, response, question="HomeSpace là gì?")
    assert result is None


@pytest.mark.asyncio
async def test_model_question_text_becomes_a_sanitized_clarification(monkeypatch):
    response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text",
                "text": "Bạn muốn **tìm** ở cơ sở nào? [link](/rent/" + ID + ")"}]}]}
    result, _ = await plan_with(monkeypatch, response)
    assert result.name == "ask_clarification"
    assert ID not in result.arguments["question"] and "*" not in result.arguments["question"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"status": "incomplete", "output": []},
    call("invented_tool", {}),
    call("search_listings", {"constraints": "bad"}),
    {"status": "completed", "output": [{"type": "function_call", "name": "search_listings", "arguments": "not JSON"}]},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
    {"status": "completed", "output": [*call("search_listings", search().arguments)["output"],
                                     *call("search_listings", search().arguments)["output"]]},
    call("ask_clarification", {"question": "", "missing": ["LOCATION"], "options": []}),
])
async def test_invalid_or_incomplete_tool_call_is_not_routed_to_rag(monkeypatch, response):
    with pytest.raises(GenerationUnavailableError):
        await plan_with(monkeypatch, response)


def test_t32_every_planner_argument_compiles_to_catalog_fields_and_operators():
    """Contract: what the planner schema allows is what Core's catalog accepts (no silently dropped args)."""
    args = search(c("amenity.ELEVATOR", "eq", "true"), c("room.restroom_type", "eq", "PRIVATE"),
                  c("rent.monthly_amount", "lte", "5000000"), c("furnishing.BED.handover_condition", "gte", "GOOD"),
                  c("furnishing.BED.total_quantity", "gte", "2"), c("policy.petPolicy.value", "in", "", values=["ALLOWED", "APPROVAL_REQUIRED"]),
                  c("street", "eq", "Đường Nguyễn Văn Bảo"), sort="rent_asc", keyword="gần chợ").arguments
    update = normalize("Tìm phòng trọ ở Hạnh Thông tối đa 5 triệu đường Nguyễn Văn Bảo", args, CAT, None, TODAY)
    state = rs.merge(None, update, "")
    payload = rs.core_request(state, limit=5)
    fields = {f["code"]: f for f in CATALOG["fields"]}
    leaves = [x for x in payload["where"]["all"] if "field" in x]
    assert len(leaves) == len(state["constraints"])
    for leaf in leaves:
        assert leaf["op"] in fields[leaf["field"]]["operators"], leaf
    assert {"field": "street", "op": "contains", "value": "Nguyễn Văn Bảo"} in leaves
    assert payload["sort"] == "rent_asc" and payload["keyword"] == "gần chợ" and payload["version"] == 2


def test_invalid_enum_and_unknown_field_are_catalog_errors():
    with pytest.raises(CatalogError) as enum:
        normalize("Tìm phòng", search(c("room.restroom_type", "eq", "PRIVATE_ONLY")).arguments, CAT, None, TODAY)
    assert enum.value.reason == "INVALID_VALUE" and "PRIVATE" in enum.value.allowed
    with pytest.raises(CatalogError):
        normalize("Tìm phòng", search(c("room.view_quality", "eq", "GOOD")).arguments, CAT, None, TODAY)


def test_parking_without_fee_words_does_not_invent_paid_parking():
    update = normalize("Tìm phòng trọ có ban công và chỗ gửi xe máy.",
                       search(c("room.balcony_type", "eq", "PRIVATE"), c("charge.MOTORBIKE_PARKING.included_in_rent", "eq", "false"),
                              c("room.parking_policy", "eq", "PAID")).arguments, CAT, None, TODAY)
    found = {(x["field"], x["op"], x["value"]) for x in update["constraints"]}
    assert ("room.has_balcony", "eq", True) in found and ("room.parking_policy", "ne", "NONE") in found
    assert not any(f.startswith("charge.MOTORBIKE_PARKING") for f, _, _ in found)
    assert ("category", "eq", "ROOM") in found


@pytest.mark.parametrize("question,kept", [
    ("Tìm phòng ở Hạnh Thông gần phòng gym", False),
    ("tim phong gan cho tap gym o An Nhon", False),
    ("Tìm căn hộ ở Hạnh Thông, tòa nhà có phòng gym", True),
    ("Căn hộ có gym, gần trường", True),
])
def test_t21_gym_in_the_building_is_an_amenity_but_a_gym_nearby_is_an_unknown_limitation(question, kept):
    update = normalize(question, search(c("amenity.GYM", "eq", "true")).arguments, CAT, None, TODAY)
    assert any(x["field"] == "amenity.GYM" for x in update["constraints"]) is kept
    assert any(u["code"] == "NEARBY_SPORTS" for u in update["unsupported"]) is not kept


def test_t21_uncollected_sports_group_from_the_model_is_reported_not_sent_to_core():
    update = normalize("Tìm phòng gần sân thể thao ở Hạnh Thông",
                       search(nearby=[{"kind": None, "group": "SPORTS", "radiusMeters": 1000}]).arguments, CAT, None, TODAY)
    assert update["nearby"] == [] and [u["code"] for u in update["unsupported"]] == ["NEARBY_SPORTS"]
    assert "NEARBY_SPORTS" in {u["code"] for u in CAT.unsupported}


def test_ward_name_sent_as_a_landmark_is_the_area_and_text_location_predicates_are_not_sent():
    question = "Mình cần thuê ở Hạnh Thông, Hồ Chí Minh, cho ba người, không quá 4 triệu, WC riêng và có cửa sổ."
    args = search(c("ward", "contains", "Hạnh Thông"), c("city", "eq", "Hồ Chí Minh"), c("room.has_window", "eq", "true"),
                  place="Hạnh Thông").arguments
    update = normalize(question, args, CAT, None, TODAY)
    assert update["placeQuery"] is None
    assert update["location"] == {"ward": "Hạnh Thông", "city": "Hồ Chí Minh"}
    assert not any(x["field"] in {"ward", "city", "ward_id", "city_id"} for x in update["constraints"])


@pytest.mark.parametrize("question,place,kept", [
    ("Ở Hạnh Thông có phòng trọ nào tiền thuê dưới 4 triệu không?", "Trường Tiểu Học Hạnh Thông", False),
    ("Mình tìm phòng ở An Nhơn có thang máy", "An Nhơn", False),
    ("Tìm phòng gần IUH dưới 5 triệu", "IUH Nguyễn Văn Bảo", True),
    ("Tìm phòng cách chợ Hạnh Thông Tây 1 km", "Chợ Hạnh Thông Tây", True),
    ("Phòng nào đi bộ tới Đại học Văn Lang được?", "Đại học Văn Lang", True),
])
def test_only_a_landmark_the_customer_named_or_asked_to_be_near_becomes_a_target(question, place, kept):
    update = normalize(question, search(place=place, ward=None, city=None).arguments, CAT, None, TODAY)
    assert (update["placeQuery"] is not None) is kept


def test_generic_phong_adds_no_category_and_occupants_are_minimum_capacity():
    update = normalize("Tìm phòng cho 1 người", search(c("category", "eq", "ROOM"), c("room.max_occupants", "eq", "1")).arguments,
                       CAT, None, TODAY)
    assert not any(x["field"] == "category" for x in update["constraints"])
    assert {"field": "occupancy.max_occupants", "op": "gte", "value": 1, "strength": "hard"} in update["constraints"]
    assert update["context"] == {"occupants": 1}


@pytest.mark.parametrize("question,expected", [
    ("Tìm trọ tối đa 4 triệu rưỡi", [("lte", 4500000)]),
    ("tim phong toi da 4tr5", [("lte", 4500000)]),
    ("dưới 3tr2 nha", [("lt", 3200000)]),
    ("từ 3 đến 4 triệu rưỡi", [("gte", 3000000), ("lte", 4500000)]),
    ("không quá 500m tới trường", []),
    ("rộng ít nhất 30m2, tối đa 5 củ", [("lte", 5000000)]),
    ("tụi mình ba người, mỗi người tối đa 1,5 triệu", [("lte", 4500000)]),
    ("mỗi người tối đa 1,5 triệu", []),
    ("tổng không quá 4 triệu rưỡi, chứ không phải mỗi người 4 triệu rưỡi", [("lte", 4500000)]),
    ("nước tính theo mét khối, đơn giá tối đa 25 nghìn", []),
    ("tiền phòng tối đa 4tr, phí gửi xe tối đa 100k", [("lte", 4000000)]),
    ("cọc tối đa 5 triệu", []),
    ("có điện nước giá dân, tối đa 4 triệu", [("lte", 4000000)]),
    ("tổng chi phí tối đa 5 triệu", [("lte", 5000000)]),
])
def test_budget_words_halves_and_per_person_amounts(question, expected):
    from homespace_ai.application.renter_criteria import budget_bounds, occupants
    assert [(op, int(v)) for op, v in budget_bounds(question, occupants(question))] == expected


def test_text_budget_never_overrides_the_model_with_a_truncated_amount():
    update = normalize("Tìm trọ ở An Nhơn, tối đa 4 triệu rưỡi", search(c("rent.monthly_amount", "lte", "4500000")).arguments, CAT, None, TODAY)
    assert [(x["field"], x["op"], x["value"]) for x in update["constraints"] if x["field"] == "rent.monthly_amount"] == [
        ("rent.monthly_amount", "lte", "4500000")]


def test_added_person_counts_from_current_state():
    state = rs.merge(None, {"context": {"occupants": 2}}, "")
    assert normalize("Thêm 1 người nữa", search().arguments, CAT, state, TODAY)["context"]["occupants"] == 3
    assert normalize("Thêm 1 người thành 4 người", search().arguments, CAT, state, TODAY)["context"]["occupants"] == 4
