from types import SimpleNamespace

import pytest

from homespace_ai.application.renter_recommendation import parse_radius, parse_request, parse_target, wants_nearby
from homespace_ai.application.use_cases.listing_ask import ListingAsk
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner, ToolPlan, _SURROUNDINGS_PARAMETERS
from homespace_ai.tools.listings import ListingToolError


def uid(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


def core_response(recommended, status="OK", target=None, radius=3000):
    ranking = [{"listingId": value, "rank": index, "originalPosition": None, "score": 90 - index,
                "reasons": [{"category": "SUPERMARKET", "label": "siêu thị", "poiName": "WinMart", "distanceMeters": 300 + index * 100}],
                "missing": []} for index, value in enumerate(recommended, 1)]
    return {"status": status, "candidateCount": len(recommended), "comparedCount": len(recommended), "radiusMeters": radius,
            "recommendedIds": recommended, "ranking": ranking, "candidates": [],
            "target": target or {"query": None, "status": "NOT_REQUESTED", "options": []},
            "sources": [{"name": "Geoapify", "attribution": "Geoapify; © OpenStreetMap contributors"}]}


class Tools:
    def __init__(self, ids=None, response=None, error=None):
        self.ids = ids or []
        self.response = response
        self.error = error
        self.compare_calls = []

    async def fields(self):
        return {"price_amount": "NUMBER"}

    async def search(self, arguments):
        return {"listingIds": self.ids, "total": len(self.ids), "readAt": "now"}

    async def detail(self, listing_id):
        number = int(listing_id[-12:])
        return {"id": listing_id, "title": f"Phòng {number}", "category": "ROOM",
                "pricing": {"amount": 3000000 + number * 100000, "unit": "ROOM_MONTH"},
                "address": {"display": "Phường A"}}

    async def compare_surroundings(self, arguments):
        self.compare_calls.append(arguments)
        if self.error:
            raise self.error
        if callable(self.response):
            return self.response(arguments)
        return self.response or core_response(arguments["listingIds"][:3])


class Planner:
    def __init__(self, plan=None):
        self.plan_result = plan

    async def plan(self, question, history, fields):
        return self.plan_result


def assistant(tools, plan=None):
    item = ListingAsk.__new__(ListingAsk)
    item.settings = SimpleNamespace()
    item.tools = tools
    item.planner = Planner(plan)
    item.generative_client = SimpleNamespace()
    return item


def scope_history(count):
    return [{"role": "assistant", "content": "Danh sách", "status": "ANSWERED",
             "listingContext": {"version": 1, "listingIds": [uid(n) for n in range(1, count + 1)]}}]


@pytest.mark.parametrize("text,expected", [
    ("trong bán kính 1,5km", 1500), ("500m thôi", 500), ("cách 2 km", 2000), ("1.5 km", 1500),
    ("phòng dưới 5 triệu", None), ("ở 3 người", None), ("phòng 30 m²", None), ("phòng 30m2", None), ("3tr 20m2", None),
])
def test_radius_parser_only_accepts_distance_units(text, expected):
    assert parse_radius(text) == (expected, None)


def test_radius_is_rejected_not_clamped():
    assert parse_radius("bán kính 20km")[0] is None and "tối đa" in parse_radius("bán kính 20km")[1]
    assert parse_radius("bán kính 0 km")[1] is not None


def test_request_parsing_profiles_priorities_target():
    parsed = parse_request("Tìm phòng gần Đại học Văn Lang cho sinh viên, gần siêu thị và bệnh viện trong 2km")
    assert parsed.profiles == ["STUDENT"]
    assert {"SUPERMARKET", "HOSPITAL", "UNIVERSITY"} <= set(parsed.priorities)
    assert parsed.radius_meters == 2000
    assert parsed.target == "Đại học Văn Lang"
    assert parse_target("phòng ở đại học") is None
    assert parse_request("gia đình có con nhỏ").profiles == ["FAMILY_WITH_CHILDREN"]


def test_plain_landmark_search_is_not_hijacked_by_nearby_flow():
    question = "Tìm phòng gần ĐH Quốc gia TP.HCM"
    assert not wants_nearby(question, parse_request(question), None)
    question = "Phòng nào gần bệnh viện phù hợp gia đình có con nhỏ?"
    assert wants_nearby(question, parse_request(question), None)


def test_surroundings_tool_schema_is_strict_and_enumerated():
    assert _SURROUNDINGS_PARAMETERS["additionalProperties"] is False
    assert set(_SURROUNDINGS_PARAMETERS["required"]) == set(_SURROUNDINGS_PARAMETERS["properties"])
    assert "STUDENT" in _SURROUNDINGS_PARAMETERS["properties"]["renterProfiles"]["items"]["enum"]
    assert _SURROUNDINGS_PARAMETERS["properties"]["listingIds"]["maxItems"] == 10
    ToolPlan("x", {})
    assert ListingToolPlanner._validated("compare_listing_surroundings", {
        "listingIds": [uid(1)], "renterProfiles": ["STUDENT"], "priorityCategories": [], "radiusMeters": None,
        "targetPlaceQuery": None}).name == "compare_listing_surroundings"
    with pytest.raises(ValueError):
        ListingToolPlanner._validated("compare_listing_surroundings", {
            "listingIds": [uid(1)], "renterProfiles": ["ALIEN"], "priorityCategories": []})


@pytest.mark.asyncio
async def test_model_or_core_returning_four_still_shows_at_most_three():
    tools = Tools(response=core_response([uid(1), uid(2), uid(3), uid(4)]))
    result = await assistant(tools).try_answer("Nên chọn phòng nào gần siêu thị cho sinh viên?", scope_history(5), "r")
    assert result.listing_ids == [uid(1), uid(2), uid(3)]
    assert result.listing_context["listingIds"] == [uid(n) for n in range(1, 6)]  # original positions stay stable
    assert result.listing_positions[uid(3)] == 3
    assert "WinMart" in result.answer and "chim bay" not in result.answer and "OpenStreetMap" in result.answer
    assert result.renter_context["profiles"] == ["STUDENT"] and result.renter_context["priorities"] == ["SUPERMARKET"]


@pytest.mark.asyncio
async def test_plain_browse_of_ten_is_not_capped_by_the_recommendation_limit():
    ids = [uid(n) for n in range(1, 11)]
    plan = ToolPlan("search_listings", {"filters": []})
    result = await assistant(Tools(ids=ids), plan).try_answer("Tìm 10 phòng trọ", [], "r")
    assert result.listing_ids == ids


@pytest.mark.asyncio
async def test_follow_up_compares_only_the_selected_rooms_and_keeps_positions():
    tools = Tools()
    result = await assistant(tools).try_answer("So sánh phòng 2 và phòng 4 gần siêu thị cho sinh viên", scope_history(5), "r")
    call = tools.compare_calls[0]
    assert call["listingIds"] == [uid(2), uid(4)]
    assert call["originalPositions"] == {uid(2): 2, uid(4): 4}
    assert result.listing_ids == [uid(2), uid(4)]


@pytest.mark.asyncio
async def test_radius_and_profile_survive_beyond_the_eight_message_window():
    state = {"version": 1, "profiles": ["FAMILY_WITH_CHILDREN"], "priorities": ["HOSPITAL"], "radiusMeters": 5000,
             "targetPlace": None, "candidateIds": [uid(1), uid(2), uid(3)], "pending": None}
    tools = Tools()
    history = [{"role": "user", "content": "chuyện khác"}] * 8  # no listing context left in the 8-message window
    result = await assistant(tools).try_answer("Ưu tiên thêm siêu thị thì phòng nào hợp?", history, "r", renter_context=state)
    call = tools.compare_calls[0]
    assert call["radiusMeters"] == 5000
    assert call["renterProfiles"] == ["FAMILY_WITH_CHILDREN"]
    assert call["priorityCategories"] == ["HOSPITAL", "SUPERMARKET"]
    assert call["listingIds"] == [uid(1), uid(2), uid(3)]
    assert result.renter_context["radiusMeters"] == 5000


@pytest.mark.asyncio
async def test_narrowing_radius_keeps_profile_priorities_and_candidates():
    state = {"version": 1, "profiles": ["STUDENT"], "priorities": ["SUPERMARKET"], "radiusMeters": 5000,
             "targetPlace": None, "candidateIds": [uid(1), uid(2)], "pending": None}
    tools = Tools()
    result = await assistant(tools).try_answer("Chỉ trong 1km thôi", scope_history(2), "r", renter_context=state)
    call = tools.compare_calls[0]
    assert (call["radiusMeters"], call["renterProfiles"], call["priorityCategories"]) == (1000, ["STUDENT"], ["SUPERMARKET"])
    assert result.renter_context["radiusMeters"] == 1000


@pytest.mark.asyncio
async def test_invalid_radius_is_refused_before_calling_core():
    tools = Tools()
    result = await assistant(tools).try_answer("Phòng nào gần siêu thị cho sinh viên trong bán kính 50km?", scope_history(3), "r")
    assert not tools.compare_calls and "tối đa" in result.answer and not result.listing_ids


@pytest.mark.asyncio
async def test_unknown_renter_need_asks_instead_of_guessing():
    tools = Tools()
    result = await assistant(tools).try_answer("Tiện ích xung quanh các phòng này thế nào?", scope_history(3), "r")
    assert not tools.compare_calls
    assert "bạn thuê cho ai" in result.answer
    assert result.renter_context["pending"] == "PROFILE"
    assert result.renter_context["candidateIds"] == [uid(1), uid(2), uid(3)]


@pytest.mark.asyncio
async def test_pending_profile_reply_completes_the_comparison():
    state = {"version": 1, "profiles": [], "priorities": [], "radiusMeters": None, "targetPlace": None,
             "candidateIds": [uid(1), uid(2)], "pending": "PROFILE"}
    tools = Tools()
    result = await assistant(tools).try_answer("sinh viên", scope_history(2), "r", renter_context=state)
    assert tools.compare_calls[0]["renterProfiles"] == ["STUDENT"]
    assert result.renter_context["pending"] is None


@pytest.mark.asyncio
async def test_geoapify_not_configured_never_claims_a_top_three():
    tools = Tools(response=core_response([], status="GEOAPIFY_NOT_CONFIGURED"))
    result = await assistant(tools).try_answer("Nên chọn phòng nào gần siêu thị cho sinh viên?", scope_history(3), "r")
    assert result.listing_ids == []
    assert "chưa bật" in result.answer and "top 3" not in result.answer.lower()
    assert result.listing_context["listingIds"] == [uid(1), uid(2), uid(3)]


@pytest.mark.asyncio
async def test_provider_outage_and_core_rate_limit_are_honest_fallbacks():
    outage = await assistant(Tools(error=ListingToolError("x", status_code=502))).try_answer(
        "Nên chọn phòng nào gần siêu thị cho sinh viên?", scope_history(3), "r")
    assert outage.listing_ids == [] and "chưa lấy được" in outage.answer
    limited = await assistant(Tools(error=ListingToolError("x", status_code=429))).try_answer(
        "Nên chọn phòng nào gần siêu thị cho sinh viên?", scope_history(3), "r")
    assert limited.listing_ids == [] and "thử lại" in limited.answer


@pytest.mark.asyncio
async def test_guest_gets_a_login_message_and_no_core_call():
    tools = Tools()
    result = await assistant(tools).try_answer("Nên chọn phòng nào gần siêu thị cho sinh viên?", scope_history(3), "r", is_guest=True)
    assert not tools.compare_calls and "đăng nhập" in result.answer


@pytest.mark.asyncio
async def test_ambiguous_target_asks_for_confirmation_and_the_reply_is_used():
    target = {"query": "Văn Lang", "status": "NEEDS_CONFIRMATION",
              "options": [{"name": "Đại học Văn Lang"}, {"name": "Đại học Văn Lang - Cơ Sở 2"}]}
    tools = Tools(response=core_response([], target=target))
    result = await assistant(tools).try_answer("Phòng nào gần đại học Văn Lang hợp sinh viên?", scope_history(3), "r")
    assert "Cơ Sở 2" in result.answer and result.listing_ids == []
    assert result.renter_context["pending"] == "TARGET"
    follow = Tools()
    reply = await assistant(follow).try_answer("Đại học Văn Lang - Cơ Sở 2", scope_history(3), "r", renter_context=result.renter_context)
    assert follow.compare_calls[0]["targetPlaceQuery"] == "Đại học Văn Lang - Cơ Sở 2"
    assert reply.renter_context["pending"] is None


@pytest.mark.asyncio
async def test_new_search_chain_replaces_candidates_and_then_compares():
    ids = [uid(7), uid(8), uid(9)]
    plan = ToolPlan("search_listings", {"filters": []})
    tools = Tools(ids=ids)
    old = {"version": 1, "profiles": ["STUDENT"], "priorities": [], "radiusMeters": 2000, "targetPlace": None,
           "candidateIds": [uid(1)], "pending": None}
    result = await assistant(tools, plan).try_answer("Tìm phòng quận 1 phù hợp sinh viên gần siêu thị", scope_history(1), "r", renter_context=old)
    assert tools.compare_calls[0]["listingIds"] == ids
    assert tools.compare_calls[0]["radiusMeters"] == 2000
    assert result.renter_context["candidateIds"] == ids


@pytest.mark.asyncio
async def test_plain_new_search_replaces_remembered_candidates_but_keeps_preferences():
    state = {"version": 1, "profiles": ["STUDENT"], "priorities": [], "radiusMeters": 2000, "targetPlace": None,
             "candidateIds": [uid(1)], "pending": None}
    plan = ToolPlan("search_listings", {"filters": []})
    result = await assistant(Tools(ids=[uid(5)]), plan).try_answer("Tìm phòng trọ cho tôi xem", [], "r", renter_context=state)
    assert result.renter_context["candidateIds"] == [uid(5)] and result.renter_context["profiles"] == ["STUDENT"]


@pytest.mark.asyncio
async def test_renter_memory_write_is_owner_scoped():
    from homespace_ai.repositories.conversation_repo import ConversationRepository
    seen = {}

    class Collection:
        async def update_one(self, query, update):
            seen["query"], seen["update"] = query, update
            return SimpleNamespace(matched_count=1)

    repository = ConversationRepository.__new__(ConversationRepository)
    repository.collection = Collection()
    assert await repository.set_renter_context("user-1", "conv-1", {"version": 1})
    assert "user-1" in str(seen["query"]) and seen["query"]["_id"] == "conv-1"
    assert seen["update"] == {"$set": {"renterRecommendationContext": {"version": 1}}}


def test_search_stage_keeps_listing_criteria_and_drops_role_and_amenity_wishes():
    from homespace_ai.application.renter_recommendation import search_text
    text = search_text("Tìm giúp mình phòng trọ ở phường An Nhơn phù hợp sinh viên, gần siêu thị, dưới 3,5 triệu.")
    assert "An Nhơn" in text and "dưới 3,5 triệu" in text
    assert "sinh viên" not in text and "siêu thị" not in text
    assert "Văn Lang" not in search_text("tìm phòng ở Gò Vấp gần Đại học Văn Lang cho sinh viên trong bán kính 2km")
    assert "2km" not in search_text("tìm phòng ở Gò Vấp gần siêu thị trong bán kính 2km")


@pytest.mark.asyncio
async def test_search_uses_only_listing_criteria_without_keyword_then_compares():
    seen = {}

    class SpyPlanner(Planner):
        async def plan(self, question, history, fields):
            seen["question"] = question
            return ToolPlan("search_listings", {"filters": [], "keyword": "siêu thị"})

    class SpyTools(Tools):
        async def search(self, arguments):
            seen["arguments"] = arguments
            return await super().search(arguments)

    tools = SpyTools(ids=[uid(1), uid(2)])
    item = assistant(tools)
    item.planner = SpyPlanner()
    result = await item.try_answer("Tìm phòng trọ ở phường An Nhơn phù hợp sinh viên, gần siêu thị, dưới 3,5 triệu.", [], "r")
    assert "siêu thị" not in seen["question"] and "sinh viên" not in seen["question"]
    assert seen["arguments"]["keyword"] is None
    assert tools.compare_calls[0]["listingIds"] == [uid(1), uid(2)]
    assert tools.compare_calls[0]["renterProfiles"] == ["STUDENT"]
    assert result.listing_ids


@pytest.mark.asyncio
async def test_location_clarification_answer_resumes_the_recommendation_workflow():
    from homespace_ai.application.listing_location import LocationClarification

    class AskPlanner(Planner):
        calls = 0

        async def plan(self, question, history, fields):
            AskPlanner.calls += 1
            if "Hồ Chí Minh" not in question:
                raise LocationClarification("Tên phường chưa xác định duy nhất.")
            return ToolPlan("search_listings", {"filters": []})

    tools = Tools(ids=[uid(1), uid(2)])
    item = assistant(tools)
    item.planner = AskPlanner()
    first = await item.try_answer("Tìm phòng trọ ở phường An Nhơn phù hợp sinh viên, gần siêu thị, dưới 3,5 triệu.", [], "r")
    assert first.renter_context["pending"] == "SEARCH" and first.renter_context["profiles"] == ["STUDENT"]
    second = await item.try_answer("Hồ Chí Minh", [], "r", renter_context=first.renter_context)
    assert tools.compare_calls and tools.compare_calls[0]["priorityCategories"] == ["SUPERMARKET"]
    assert second.listing_ids and second.renter_context["pending"] is None


@pytest.mark.asyncio
async def test_found_rooms_are_shown_even_when_the_amenity_step_cannot_rank_them():
    ids = [uid(1), uid(2), uid(3), uid(4)]
    plan = ToolPlan("search_listings", {"filters": []})
    # Map data switched off: step 1 cards stay, no top-3 claim.
    tools = Tools(ids=ids, response=core_response([], status="GEOAPIFY_NOT_CONFIGURED"))
    result = await assistant(tools, plan).try_answer("Tìm phòng trọ ở phường An Nhơn phù hợp sinh viên, gần siêu thị", [], "r")
    assert result.listing_ids == ids and "chưa bật" in result.answer and "4 phòng khớp tiêu chí" in result.answer
    # Role unknown: still show the rooms, ask who the renter is, and never call the amenity API.
    tools = Tools(ids=ids)
    result = await assistant(tools, plan).try_answer("Mình đang xem mấy phòng trọ ở Gò Vấp, xem giúp tiện ích xung quanh", [], "r")
    assert not tools.compare_calls and result.listing_ids == ids and "bạn thuê cho ai" in result.answer
    assert result.renter_context["pending"] == "PROFILE"


@pytest.mark.asyncio
async def test_amenity_wish_after_a_listing_search_ranks_the_displayed_rooms_instead_of_searching_again():
    tools = Tools()
    result = await assistant(tools).try_answer(
        "tôi cần phòng rộng một chút gần siêu thị và trường đại học", scope_history(5), "r")
    call = tools.compare_calls[0]
    assert call["listingIds"] == [uid(n) for n in range(1, 6)]
    assert call["priorityCategories"] == ["SUPERMARKET", "UNIVERSITY"]
    assert result.renter_context["wantsSpace"] is True
    assert result.listing_ids and len(result.listing_ids) <= 3
