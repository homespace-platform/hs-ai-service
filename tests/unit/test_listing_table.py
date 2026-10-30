"""Search answers carry a comparison table; asked-for field groups are rendered from full details."""
import re

import pytest

from homespace_ai.application.listing_table import listing_table, requested_sections, strip_images, strip_tables
from homespace_ai.clients.listing_tool_planner import ToolPlan
from tests.unit.listing_fakes import IDS, Conversation, make_assistant, room, search


def full_room(listing_id, **overrides):
    item = room(listing_id, availableFrom="2026-11-19", areaM2=26.0,
                pricing={"amount": 3000000, "unit": "ROOM_MONTH", "depositType": "MONTH_COUNT", "depositMonths": 1,
                         "paymentCycle": "MONTHLY", "minimumLeaseMonths": 3, "negotiable": False,
                         "managementFeeIncluded": True, "vatIncluded": False},
                roomDetail={"restroomType": "PRIVATE", "kitchenType": "NONE", "hasWindow": True, "balconyType": "PRIVATE",
                            "hasMezzanine": False, "furnishingStatus": "FULLY_FURNISHED", "maxOccupants": 2,
                            "maxVehicles": 2, "parkingPolicy": "FREE"},
                amenities=[{"code": "WIFI", "name": "WiFi"}], customAmenities=["Khu giặt phơi dùng chung"],
                furnishings=[{"itemCode": "BED", "assetName": "Giường", "quantity": 1, "handoverConditionLabel": "Còn tốt"}],
                charges=[{"chargeType": "ELECTRICITY", "billingMethod": "PER_KWH", "amount": 3425.0, "unit": "kWh", "includedInRent": False},
                         {"chargeType": "MANAGEMENT", "billingMethod": "PER_MONTH", "amount": 0.0, "unit": "tháng", "includedInRent": True}],
                policies=[{"code": "petPolicy", "label": "Nuôi thú cưng", "value": "NOT_ALLOWED"},
                          {"code": "noticeDaysBeforeTermination", "label": "Báo trước khi chấm dứt", "value": "30", "unitLabel": "ngày"}],
                viewingDays=["MONDAY", "SATURDAY"], viewingSlots=["EVENING"])
    item.update(overrides)
    return item


def plain(text):
    return re.sub(r"\[([^\[\]]+)\]\(/rent/[^)]*\)", r"\1", text)


def assistant_with(plan, details):
    assistant = make_assistant(plan, ids=list(details))
    assistant.tools.details = details
    return assistant


@pytest.mark.asyncio
async def test_search_answer_is_a_key_field_table_without_asking_for_one():
    details = {IDS[0]: full_room(IDS[0]), IDS[1]: full_room(IDS[1], roomDetail={"restroomType": "SHARED", "parkingPolicy": "NONE"})}
    assistant = assistant_with(search(ward="Hạnh Thông", city="HCM"), details)
    result = await assistant.try_answer("Tìm phòng trọ ở Hạnh Thông HCM dưới 4 triệu", [], "table")
    answer = result.answer
    assert "| Tiêu chí | Phòng 1 | Phòng 2 |" in plain(answer)
    for row in ["| Giá thuê | 3.000.000 đ/phòng/tháng |", "| Diện tích | 26 m² |", "| Có thể nhận | 19/11/2026 |",
                "| WC | Riêng | Chung |", "| Cửa sổ | Có | Chưa rõ |", "| Gửi xe máy | Tối đa 2 xe; không thu phí riêng | Không nhận gửi xe |"]:
        assert row in answer
    assert "| Tiện ích" not in answer and "| WiFi" not in answer, "Default table keeps to key fields"
    assert "thêm vào bảng" in answer and result.listing_ids == IDS[:2]
    assert assistant.generative_client.kwargs is None


@pytest.mark.asyncio
@pytest.mark.parametrize("follow_up,plan_more", [("xem thêm", True), ("xem thêm", False), ("cho 4 tin khác", True)])
async def test_more_pages_use_the_same_table_without_model_prose(follow_up, plan_more):
    details = {value: full_room(value) for value in IDS}
    plans = iter([search(ward="Hạnh Thông", city="HCM"), search(ward="Hạnh Thông", city="HCM", moreResults=plan_more)])
    assistant = assistant_with(lambda question: next(plans), details)
    talk = Conversation(assistant)
    first = await talk.ask("Tìm phòng trọ ở Hạnh Thông HCM dưới 4 triệu")
    second = await talk.ask(follow_up)
    assert first.listing_ids == IDS[:4] and second.listing_ids == IDS[4:8]
    assert assistant.tools.payloads[1]["excludeIds"] == IDS[:4]
    assert assistant.generative_client.kwargs is None
    assert "Tiêu chí đang áp dụng" in first.answer and "Tiêu chí đang áp dụng" not in second.answer
    assert "tiếp theo" in second.answer and "| Tiêu chí |" in second.answer
    assert "Gợi ý chọn" not in second.answer and "thêm vào bảng" not in second.answer
    assert second.answer.count("| Tiêu đề |") == first.answer.count("| Tiêu đề |") == 1


@pytest.mark.asyncio
async def test_asked_for_groups_use_full_details_not_the_truncated_search_preview():
    amenities = [{"code": f"A{i}", "name": f"Tiện ích {i}"} for i in range(1, 31)]
    details = {IDS[0]: full_room(IDS[0], amenities=amenities), IDS[1]: full_room(IDS[1])}
    assistant = assistant_with(search(ward="Hạnh Thông", city="HCM"), details)
    result = await assistant.try_answer("Tìm phòng ở Hạnh Thông HCM, liệt kê tiện ích và chi phí", [], "full")
    assert "| Tiện ích 30 | Có | — |" in result.answer, "The 4 KB preview keeps 24 amenities; the table must not"
    assert "| WiFi | — | Có |" in result.answer and "| Khu giặt phơi dùng chung | Có | Có |" in result.answer
    assert "| Điện | 3.425 đ/kWh | 3.425 đ/kWh |" in result.answer
    assert "| Phí quản lý | Đã gồm trong giá thuê |" in result.answer
    assert assistant.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_follow_up_make_a_table_reads_the_shown_listings_without_the_planner():
    details = {value: full_room(value) for value in IDS[:3]}
    assistant = assistant_with(None, details)
    history = [{"role": "assistant", "content": "3 phòng", "listingContext": {"version": 1, "listingIds": IDS[:3]}}]
    result = await assistant.try_answer("lập bảng so sánh trực quan", history, "followup")
    assert "| Tiêu chí | Phòng 1 | Phòng 2 | Phòng 3 |" in plain(result.answer) and "| Ban công | Riêng | Riêng | Riêng |" in result.answer
    assert assistant.planner.calls == 0 and assistant.tools.read == IDS[:3]
    assert assistant.generative_client.kwargs is None and result.listing_ids == IDS[:3]


@pytest.mark.asyncio
@pytest.mark.parametrize("question,rows", [
    ("so sánh tiện ích và nội quy các phòng trên", ["| WiFi | Có | Có |", "| Nuôi thú cưng | Không cho phép | Không cho phép |",
                                                  "| Báo trước khi chấm dứt | 30 ngày | 30 ngày |"]),
    ("thêm nội thất và lịch xem vào bảng", ["| Mức nội thất | Đầy đủ | Đầy đủ |", "| Giường | 1 cái, Còn tốt | 1 cái, Còn tốt |",
                                           "| Ngày xem | T2, T7 | T2, T7 |", "| Khung giờ xem | Tối | Tối |"]),
    ("lập bảng tiền cọc, thanh toán của 2 phòng này", ["| Tiền cọc | 1 tháng tiền thuê | 1 tháng tiền thuê |",
                                                      "| Kỳ thanh toán | Hằng tháng | Hằng tháng |", "| Thuê tối thiểu | 3 tháng | 3 tháng |"]),
])
async def test_follow_up_field_groups_are_rendered_in_the_table(question, rows):
    details = {value: full_room(value) for value in IDS[:2]}
    assistant = assistant_with(None, details)
    history = [{"role": "assistant", "content": "2 phòng", "listingContext": {"version": 1, "listingIds": IDS[:2]}}]
    result = await assistant.try_answer(question, history, "groups")
    for row in rows:
        assert row in result.answer
    assert "| Giá thuê |" in result.answer and assistant.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_recommendation_keeps_model_advice_but_the_table_is_the_servers():
    details = {value: full_room(value) for value in IDS[:2]}
    assistant = assistant_with(ToolPlan("get_listing_details", {"listingIds": IDS[:2]}), details)

    async def advice(**kwargs):
        return "Mình gợi ý Phòng 1.\n\n| Tiêu chí | Phòng 1 |\n| --- | --- |\n| Giá | 9 tỷ |\n\n- **Lý do:** WC riêng."
    assistant.generative_client.generate_answer = advice
    history = [{"role": "assistant", "content": "2 phòng", "listingContext": {"version": 1, "listingIds": IDS[:2]}}]
    result = await assistant.try_answer("Nên chọn phòng nào trong các phòng trên?", history, "advice")
    assert assistant.generative_client.evidence["tableRendered"] == ["Thông tin chính"]
    assert "9 tỷ" not in result.answer, "A model-written table is replaced by the server-rendered one"
    assert "Mình gợi ý Phòng 1." in result.answer and "| Giá thuê | 3.000.000 đ/phòng/tháng |" in result.answer


@pytest.mark.asyncio
async def test_search_filter_words_do_not_turn_into_extra_sections():
    details = {IDS[0]: full_room(IDS[0])}
    assistant = assistant_with(search(ward="Hạnh Thông", city="HCM"), details)
    result = await assistant.try_answer("Tìm phòng ở Hạnh Thông HCM có máy lạnh", [], "filter")
    assert "| Nội thất | Đầy đủ |" in result.answer and "| Giường |" not in result.answer


@pytest.mark.asyncio
async def test_conversation_search_then_amenities_table():
    details = {value: full_room(value) for value in IDS[:2]}
    assistant = assistant_with(search(ward="Hạnh Thông", city="HCM"), details)
    talk = Conversation(assistant)
    first = await talk.ask("Tìm phòng trọ ở Hạnh Thông HCM")
    second = await talk.ask("các phòng trên có tiện ích gì")
    assert "| Gửi xe máy |" in first.answer
    assert "| WiFi | Có | Có |" in second.answer and second.listing_ids == IDS[:2]


def test_mixed_categories_mark_rows_that_do_not_apply():
    house = full_room(IDS[1], category="HOUSE", roomDetail=None, houseDetail={"bedroomCount": 3, "bathroomCount": 2, "totalFloors": 4,
                                                                             "hasGarage": False, "maxOccupants": 6,
                                                                             "furnishingStatus": "BASIC"},
                      maxMotorbikeCount=2, charges=[{"chargeType": "MOTORBIKE_PARKING", "amount": 100000, "unit": "xe/tháng"}],
                      address={"ward": "Phường Gò Vấp"})
    table = listing_table([full_room(IDS[0]), house], IDS[:2])
    assert "| Tiêu chí | Tin 1 | Tin 2 |" in plain(table) and "| Loại hình | Phòng trọ | Nhà nguyên căn |" in table
    assert "| Khu vực | Phường Hạnh Thông | Phường Gò Vấp |" in table
    assert "| Phòng ngủ | — | 3 |" in table and "| WC | Riêng | — |" in table and "| Số tầng | — | 4 |" in table
    assert "| Nội thất | Đầy đủ | Cơ bản |" in table and "| Gửi xe máy | Tối đa 2 xe; không thu phí riêng | Tối đa 2 xe; 100.000 đ/xe/tháng |" in table
    assert "‘—’" in table


def test_many_listings_split_into_tables_of_four_columns_and_cells_are_safe():
    listings = [full_room(value, address={"ward": "A | B"}) for value in IDS[:7]]
    table = listing_table(listings, IDS[:7], ["location"])
    assert "| Tiêu chí | Phòng 1 | Phòng 2 | Phòng 3 | Phòng 4 |" in plain(table)
    assert "| Tiêu chí | Phòng 5 | Phòng 6 | Phòng 7 |" in plain(table) and "A / B" in table


@pytest.mark.asyncio
async def test_detail_tool_keeps_one_https_cover_so_the_table_can_show_it():
    """Regression: detail() used to drop media and leave every table cell as 'Chưa có ảnh'."""
    from unittest.mock import AsyncMock
    import httpx
    from fastapi import Request
    from homespace_ai.tools.listings import ListingTools

    listing_id = IDS[0]
    gateway = AsyncMock()
    gateway.request.return_value = httpx.Response(200, json={"result": {
        "id": listing_id, "title": "Phòng có ảnh", "category": "ROOM", "status": "PUBLISHED", "active": True,
        "pricing": {"amount": 2400000, "unit": "ROOM_MONTH"}, "areaM2": 15,
        "owner": {"displayName": "SECRET"}, "createdBy": "x",
        "media": [
            {"mediaType": "IMAGE", "url": "https://cdn.example/other.jpg", "sortOrder": 1, "storageObjectId": "obj-1"},
            {"mediaType": "IMAGE", "url": "https://cdn.example/cover.jpg", "cover": True, "sortOrder": 5, "storageObjectId": "obj-2"},
            {"mediaType": "IMAGE", "url": "javascript:alert(1)", "cover": True},
            {"mediaType": "VIDEO", "url": "https://cdn.example/v.mp4", "cover": True},
        ],
    }})
    detail = await ListingTools(gateway, Request({"type": "http", "headers": []})).detail(listing_id)
    assert "owner" not in detail and "storageObjectId" not in str(detail)
    assert detail["media"] == [{"mediaType": "IMAGE", "url": "https://cdn.example/cover.jpg", "cover": True, "sortOrder": 5}]
    assert detail["mediaCounts"] == {"images": 3, "videos": 1}
    assert f"https://cdn.example/cover.jpg" in listing_table([detail], [listing_id])


@pytest.mark.asyncio
async def test_search_answer_embeds_the_cover_image_from_detail():
    details = {IDS[0]: full_room(IDS[0], media=[{"mediaType": "IMAGE", "url": "https://cdn.example/a.jpg", "cover": True}])}
    assistant = assistant_with(search(ward="Hạnh Thông", city="HCM"), details)
    result = await assistant.try_answer("Tìm phòng trọ ở Hạnh Thông HCM", [], "cover")
    assert f"[![Ảnh Phòng 1](<https://cdn.example/a.jpg>)](/rent/{IDS[0]})" in result.answer
    assert "Chưa có ảnh" not in result.answer


def test_header_links_the_listing_and_the_first_row_is_its_cover_image():
    media = [{"mediaType": "IMAGE", "url": "https://cdn.example/2.jpg", "sortOrder": 1},
             {"mediaType": "VIDEO", "url": "https://cdn.example/v.mp4", "cover": True},
             {"mediaType": "IMAGE", "url": "https://cdn.example/cover.jpg", "cover": True, "sortOrder": 5}]
    listings = [full_room(IDS[0], title='Phòng "đẹp" [mới] | gần chợ', media=media),
                full_room(IDS[1], media=[{"mediaType": "IMAGE", "url": "javascript:alert(1)"}]),
                full_room(IDS[2], media=[{"mediaType": "IMAGE", "url": "https://cdn.example/a b).jpg"}])]
    lines = listing_table(listings, IDS[:3]).splitlines()
    assert lines[0] == (f'| Tiêu chí | [Phòng 1](/rent/{IDS[0]} "Phòng đẹp mới / gần chợ") | '
                        f'[Phòng 2](/rent/{IDS[1]} "Phòng thử nghiệm") | [Phòng 3](/rent/{IDS[2]} "Phòng thử nghiệm") |')
    assert lines[2] == "| Tiêu đề | Phòng đẹp mới / gần chợ | Phòng thử nghiệm | Phòng thử nghiệm |"
    assert lines[3] == f"| Ảnh | [![Ảnh Phòng 1](<https://cdn.example/cover.jpg>)](/rent/{IDS[0]}) | Chưa có ảnh | Chưa có ảnh |"
    assert lines[4].startswith("| Giá thuê |")


def test_model_text_cannot_embed_remote_images():
    assert strip_images("Xem ![x](https://tracker.example/p.gif) nhé") == "Xem  nhé"


def test_unstated_context_defaults_from_the_model_are_not_customer_facts():
    from homespace_ai.application.renter_criteria import normalize
    from homespace_ai.application.listing_catalog import SearchCatalog
    from tests.unit.listing_fakes import CATALOG, FIXED_NOW
    plan = search(ward="Gò Vấp", city="HCM").arguments
    plan["context"] = {"occupants": 1, "motorbikes": 0, "cars": 0, "electricityKwh": 0, "waterM3": 0}
    update = normalize("Tìm phòng trọ ở Gò Vấp, HCM dưới 4 triệu", plan, SearchCatalog(CATALOG), None, FIXED_NOW.date())
    assert update["context"] == {}
    plan["context"] = {"occupants": 2, "motorbikes": 0, "electricityKwh": 150}
    update = normalize("Tìm phòng Gò Vấp HCM cho 2 người, không có xe, dùng 150 kWh điện", plan, SearchCatalog(CATALOG), None,
                       FIXED_NOW.date())
    assert update["context"] == {"occupants": 2, "motorbikes": 0, "electricityKwh": 150}


def test_distance_row_appears_for_a_chosen_place():
    table = listing_table([full_room(IDS[0])], IDS[:1], distances={IDS[0]: 1250})
    assert "| Cách địa điểm đã chọn | 1.250 m |" in table


@pytest.mark.parametrize("question,expected", [
    ("Tìm phòng diện tích 25m2", []),
    ("phòng đối diện chợ", []),
    ("phòng nào gần tiện ích xung quanh", []),
    ("tiền điện nước các phòng", ["charges"]),
    ("so sánh tiện ích", ["amenities"]),
    ("có cho nuôi mèo không", ["policies"]),
    ("cho tôi xem tất cả thông tin", ["amenities", "furnishings", "charges", "terms", "policies", "viewing", "location"]),
])
def test_requested_sections(question, expected):
    assert requested_sections(question) == expected


def test_generic_show_search_request_does_not_turn_previous_owned_furniture_need_into_inventory():
    question = ("Mình cần thuê phòng trọ cho 2 người, tiền thuê dưới 3,5 triệu. "
                "Bọn mình đã có giường tủ nên ưu tiên phòng rộng, nội thất không quan trọng. Cho mình xem 4 phòng mới đăng.")
    assert requested_sections(question, search_page=True) == []
    assert requested_sections(question + " Cho mình xem chi phí và nội thất trong bảng.", search_page=True) == ["furnishings", "charges"]


@pytest.mark.parametrize("question,scoped", [
    ("lập bảng so sánh trực quan", True),
    ("so sánh các phòng trên theo tiêu chí liên quan tới chi phí", True),
    ("thanh toán và tiền cọc của các phòng này thế nào", True),
    ("so sánh với phòng ở quận 7", False),
    ("so sánh phòng ở quan 7", False),
    ("so sánh phòng tỉnh khác", False),
])
def test_table_follow_ups_stay_on_the_shown_listings(question, scoped):
    from homespace_ai.application.listing_context import scoped_selection
    history = [{"role": "assistant", "content": "3 phòng", "listingContext": {"version": 1, "listingIds": IDS[:3]}}]
    assert (scoped_selection(question, history) == IDS[:3]) is scoped


def test_strip_tables_keeps_prose():
    assert strip_tables("Gợi ý.\n\n| a | b |\n| - | - |\n\n- Ý 1") == "Gợi ý.\n\n- Ý 1"
