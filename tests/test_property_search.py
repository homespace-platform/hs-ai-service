import os
from types import SimpleNamespace

import pytest

from homespace_ai.property_search.intent import fallback_intent, _merge_history_intent, parse_intent
from homespace_ai.property_search.field_qa import answer_listing_question, is_detail_followup
from homespace_ai.property_search.mcp_client import call_listing_tool
from homespace_ai.property_search.response import (
    compose_search_reply,
    previous_result_ids,
    references_previous_results,
)
from homespace_ai.clients.generative import DisabledGenerativeClient
from homespace_ai.clients.generative import BaseGenerativeClient


def test_vietnamese_fallback_extracts_room_features_and_budget():
    intent = fallback_intent("Tôi là sinh viên tìm phòng trọ có gác, ban công giá dưới 3tr")
    assert intent.category == "ROOM"
    assert intent.price_max == 3_000_000
    assert intent.has_mezzanine is True
    assert intent.has_balcony is True


def test_fallback_keeps_go_vap_without_inventing_distance():
    intent = fallback_intent("trọ gần đại học công nghiệp gò vấp, có gác dưới 3tr")
    assert intent.location == "Gò Vấp"
    assert intent.landmark is not None


def test_garage_negation_and_new_search_do_not_reuse_old_filter():
    first = fallback_intent("Tìm nhà nguyên căn có gara dưới 10 triệu ở TP.HCM")
    assert first.category == "HOUSE" and first.has_garage is True
    second = _merge_history_intent(
        "Tìm nhà nguyên căn không gara dưới 10 triệu ở TP.HCM",
        ["Tìm nhà nguyên căn có gara dưới 10 triệu ở TP.HCM"],
    )
    assert second.category == "HOUSE" and second.has_garage is False
    assert second.has_parking is False
    assert second.price_max == 10_000_000
    assert _merge_history_intent("Bỏ yêu cầu gara", [
        "Tìm nhà nguyên căn có gara dưới 10 triệu ở TP.HCM"
    ]).has_garage is None
    assert fallback_intent("Tìm nhà nguyên căn không có gara").has_garage is False
    assert fallback_intent("Tìm nhà nguyên căn không cần gara").has_garage is None


def test_fresh_search_does_not_inherit_other_category_features():
    intent = _merge_history_intent(
        "Tìm nhà nguyên căn không gara dưới 10 triệu",
        ["Tìm phòng trọ có gác và ban công dưới 3 triệu"],
    )
    assert intent.category == "HOUSE" and intent.has_garage is False
    assert intent.has_mezzanine is False and intent.has_balcony is False
    assert intent.price_max == 10_000_000


@pytest.mark.asyncio
async def test_explicit_no_garage_overrides_stale_model_output(monkeypatch):
    class _StaleModel:
        async def generate_answer(self, question, context_chunks, *, mode):
            return ('{"category":"HOUSE","price_max":10000000,'
                    '"has_parking":true,"has_garage":true}')

    monkeypatch.setattr("homespace_ai.property_search.intent.get_settings",
                        lambda: SimpleNamespace(generation_provider="groq"))
    monkeypatch.setattr("homespace_ai.property_search.intent.get_generative_client",
                        lambda settings: _StaleModel())
    intent = await parse_intent(
        "Tìm nhà nguyên căn không gara dưới 10 triệu ở TP.HCM",
        previous_user_messages=["Tìm nhà nguyên căn có gara dưới 10 triệu ở TP.HCM"],
    )
    assert intent.has_garage is False and intent.has_parking is False


def test_follow_up_can_remove_an_earlier_search_filter():
    prior = ["Tìm phòng trọ có gác và ban công dưới 3,5 triệu ở Bình Thạnh"]
    intent = _merge_history_intent("Bỏ yêu cầu ban công", prior)
    assert intent.has_mezzanine is True
    assert intent.has_balcony is False
    assert intent.price_max == 3_500_000
    later = _merge_history_intent("Có chỗ gửi xe không?", [*prior, "Bỏ yêu cầu ban công"])
    assert later.has_balcony is False
    assert later.has_parking is True


def test_follow_up_uses_only_previous_search_results():
    history = [{
        "role": "assistant", "status": "PROPERTY_SEARCH",
        "content": "- [Tin A](/rent/c3822603-ae07-e05c-a8f3-ec2da14e0352)",
    }]
    ids = previous_result_ids(None, history)
    assert ids == ["c3822603-ae07-e05c-a8f3-ec2da14e0352"]
    assert references_previous_results(
        "Trong khu vực đó còn phòng nào có chỗ gửi xe không?", bool(ids), False
    )
    assert not references_previous_results("Tìm phòng ở Gò Vấp", bool(ids), True)


def test_detail_question_is_not_sent_back_to_listing_search():
    assert is_detail_followup("Phòng nào gửi xe miễn phí?", True)
    assert is_detail_followup("So sánh hai phòng đó về giá thuê và chỗ gửi xe", True)
    assert is_detail_followup("Căn hộ này ở tầng mấy, hướng ban công nào?", True)
    assert is_detail_followup("Chủ nhà tên gì, liên hệ qua số nào?", True)
    assert is_detail_followup("Phòng P160 giá điện bao nhiêu?", True)
    assert is_detail_followup("Cho thuê toàn bộ nhà hay chỉ một số tầng?", True)
    assert is_detail_followup("Chỗ đó có cách âm tốt không?", True)
    assert not is_detail_followup("Tìm thêm phòng ở Gò Vấp", True)
    assert not is_detail_followup("Cho tôi thêm lựa chọn khác?", True)
    assert not is_detail_followup("Bỏ yêu cầu ban công", True)


def _room_facts():
    return {"listings": [
        {"id": "c3822603-ae07-e05c-a8f3-ec2da14e0352",
         "listing": {"title": "Phòng A", "category": "ROOM", "price_amount": 3_200_000},
         "room": {"restroom_type": "SHARED", "kitchen_type": "PRIVATE",
                  "parking_policy": "FREE", "max_vehicles": 9},
         "charges": [{"charge_type": "MOTORBIKE_PARKING", "amount": 80_000,
                      "unit": "xe/tháng", "included_in_rent": False}]},
        {"id": "f199ecd2-5191-4d34-91c8-81a1f5a7e343",
         "listing": {"title": "Phòng B", "category": "ROOM", "price_amount": 3_200_000},
         "room": {"restroom_type": "PRIVATE", "kitchen_type": "SHARED",
                  "parking_policy": "PAID", "max_vehicles": 10},
         "charges": [{"charge_type": "MOTORBIKE_PARKING", "amount": 80_000,
                      "unit": "xe/tháng", "included_in_rent": False}]},
    ]}


@pytest.mark.asyncio
async def test_fact_answer_uses_actual_price_and_flags_fee_conflict():
    answer = await answer_listing_question(
        question="So sánh hai phòng đó về giá thuê và chỗ gửi xe. Phòng nào phải trả phí?",
        facts=_room_facts(), previous_questions=["phòng nào có chỗ gửi xe?"],
        generative_client=_HallucinatingClient(),
    )
    assert answer.count("3.200.000 đ") == 2
    assert "3.400.000" not in answer
    assert "dữ liệu mâu thuẫn" in answer
    assert "80.000 đ/xe/tháng" in answer
    assert "Phòng A" in answer and "Phòng B" in answer


@pytest.mark.asyncio
async def test_room_wc_and_free_only_are_read_from_detail_fields():
    wc = await answer_listing_question(
        question="Trong hai phòng, phòng nào WC riêng?", facts=_room_facts(),
        previous_questions=[], generative_client=DisabledGenerativeClient(),
    )
    assert "nhà vệ sinh: dùng chung" in wc
    assert "nhà vệ sinh: riêng" in wc
    free = await answer_listing_question(
        question="Nếu tôi chỉ chọn phòng gửi xe miễn phí, còn phòng nào?",
        facts=_room_facts(), previous_questions=[],
        generative_client=DisabledGenerativeClient(),
    )
    assert "chưa xác nhận được phòng nào gửi xe miễn phí" in free
    assert "Phòng A" in free and "Phòng B" not in free


@pytest.mark.asyncio
async def test_specific_room_and_owner_fields_do_not_mix_with_other_listing():
    facts = _room_facts()
    facts["listings"][0]["room"]["room_code"] = "P112"
    facts["listings"][1]["room"]["room_code"] = "P160"
    facts["listings"][1]["owner"] = {
        "display_name": "System Administrator", "phone": "0999999999",
    }
    answer = await answer_listing_question(
        question="Phòng P160 chủ nhà là ai, số điện thoại liên hệ?",
        facts=facts, previous_questions=[],
        generative_client=DisabledGenerativeClient(),
    )
    assert "Phòng B" in answer and "Phòng A" not in answer
    assert "System Administrator" in answer and "0999999999" in answer


@pytest.mark.asyncio
async def test_area_question_does_not_pull_electricity_fee():
    facts = _room_facts()
    facts["listings"][0]["listing"]["area_m2"] = 28
    facts["listings"][0]["charges"].append({
        "charge_type": "ELECTRICITY", "amount": 3000,
        "unit": "kWh", "included_in_rent": False,
    })
    answer = await answer_listing_question(
        question="Phòng đầu diện tích bao nhiêu?", facts=facts,
        previous_questions=[], generative_client=DisabledGenerativeClient(),
    )
    assert "28 m²" in answer and "Phòng B" not in answer
    assert "3.000 đ/kWh" not in answer


@pytest.mark.asyncio
async def test_custom_monthly_charges_keep_each_own_amount():
    facts = {"listings": [_room_facts()["listings"][0]]}
    facts["listings"][0]["charges"].extend([
        {"charge_type": "OTHER", "custom_name": "Phí giặt", "amount": 50_000,
         "unit": "tháng", "included_in_rent": False},
        {"charge_type": "OTHER", "custom_name": "Phí vệ sinh", "amount": 20_000,
         "unit": "tháng", "included_in_rent": False},
    ])
    answer = await answer_listing_question(
        question="Các khoản phí hàng tháng của phòng đầu là gì?",
        facts=facts, previous_questions=[],
        generative_client=DisabledGenerativeClient(),
    )
    assert "Phí giặt: 50.000 đ/tháng" in answer
    assert "Phí vệ sinh: 20.000 đ/tháng" in answer


@pytest.mark.asyncio
async def test_apartment_and_house_specific_fields_are_not_confused():
    facts = {"listings": [
        {"id": "f0cb3bc8-0b92-87cb-00ed-79963b48baa6",
         "listing": {"title": "Căn hộ A", "category": "APARTMENT", "price_amount": 11_750_000},
         "apartment": {"project_name": "Sunrise City", "floor_number": 9,
                       "balcony_direction": "Đông Nam", "bedroom_count": 3},
         "room": None, "house": None, "charges": []},
        {"id": "6eb41796-4e76-fc57-7c9b-a7570aeb98fa",
         "listing": {"title": "Nhà A", "category": "HOUSE", "price_amount": 9_000_000},
         "house": {"has_garage": True, "land_area_m2": 158,
                   "bedroom_count": 2}, "room": None, "apartment": None, "charges": []},
    ]}
    apartment = await answer_listing_question(
        question="Căn hộ ở dự án nào, tầng mấy và hướng ban công?",
        facts={"listings": facts["listings"][:1]}, previous_questions=[],
        generative_client=DisabledGenerativeClient(),
    )
    assert "Sunrise City" in apartment and "Đông Nam" in apartment and "9" in apartment
    house = await answer_listing_question(
        question="Nhà nguyên căn có gara không, diện tích đất bao nhiêu?",
        facts={"listings": facts["listings"][1:]}, previous_questions=[],
        generative_client=DisabledGenerativeClient(),
    )
    assert "gara để xe: có" in house and "158 m²" in house


@pytest.mark.asyncio
async def test_house_rental_scope_followup_reads_existing_houses_not_new_search():
    facts = {"listings": [
        {"id": "6eb41796-4e76-fc57-7c9b-a7570aeb98fa",
         "listing": {"title": "Nhà nguyên căn có sân để xe", "category": "HOUSE"},
         "house": {"rental_scope_description": "Cho thuê toàn bộ nhà; có thể trao đổi thời điểm bàn giao.",
                   "rented_floor_from": 1, "rented_floor_to": 2, "total_floors": 2}},
    ]}
    answer = await answer_listing_question(
        question="Cho thuê toàn bộ nhà hay chỉ một số tầng?", facts=facts,
        previous_questions=["Tìm nhà nguyên căn có gara dưới 10 triệu"],
        generative_client=DisabledGenerativeClient(),
    )
    assert "Cho thuê toàn bộ nhà" in answer
    assert "tầng được cho thuê: 1–2 trên tổng 2 tầng" in answer
    assert "Nhà nguyên căn có sân để xe" in answer
    assert "Mình thấy" not in answer


@pytest.mark.asyncio
async def test_matching_house_scopes_are_answered_once_with_both_links():
    base = {"rental_scope_description": "Cho thuê toàn bộ nhà.",
            "rented_floor_from": 1, "rented_floor_to": 2, "total_floors": 2}
    facts = {"listings": [
        {"id": "6eb41796-4e76-fc57-7c9b-a7570aeb98fa",
         "listing": {"title": "Nhà A", "category": "HOUSE"}, "house": base},
        {"id": "e9844120-f722-f4f0-4866-ea10e85fcb81",
         "listing": {"title": "Nhà B", "category": "HOUSE"}, "house": base},
    ]}
    answer = await answer_listing_question(
        question="Cho thuê toàn bộ nhà hay chỉ một số tầng?", facts=facts,
        previous_questions=[], generative_client=DisabledGenerativeClient(),
    )
    assert answer.startswith("Mình đã kiểm tra lại cả 2 nhà vừa tìm")
    assert "Các tin đều ghi: Cho thuê toàn bộ nhà" in answer
    assert answer.count("Cho thuê toàn bộ nhà") == 1
    assert "Nhà A" in answer and "Nhà B" in answer


@pytest.mark.asyncio
async def test_parking_comparison_answers_both_rooms_instead_of_free_only():
    facts = _room_facts()
    facts["listings"][0]["room"]["max_vehicles"] = 1
    facts["listings"][0]["charges"][0]["amount"] = 0
    facts["listings"][0]["charges"][0]["included_in_rent"] = True
    facts["listings"][1]["room"]["max_vehicles"] = 1
    answer = await answer_listing_question(
        question=("Hai phòng vừa tìm, phòng nào gửi xe miễn phí, phòng nào thu phí? "
                  "Mỗi phòng để tối đa mấy xe?"),
        facts=facts, previous_questions=[], generative_client=DisabledGenerativeClient(),
    )
    assert "Mình đã đối chiếu 2 tin" in answer
    assert "Phòng A" in answer and "Phòng B" in answer
    assert "gửi xe miễn phí, tối đa 1 xe" in answer
    assert "gửi xe có thu phí, tối đa 1 xe; mức phí 80.000 đ/xe/tháng" in answer
    assert "dữ liệu mâu thuẫn" not in answer


@pytest.mark.asyncio
async def test_latest_paid_parking_preference_overrides_earlier_free_mention():
    facts = _room_facts()
    facts["listings"][0]["room"]["max_vehicles"] = 1
    facts["listings"][0]["charges"][0]["amount"] = 0
    facts["listings"][0]["charges"][0]["included_in_rent"] = True
    facts["listings"][1]["room"]["max_vehicles"] = 1
    answer = await answer_listing_question(
        question="Phòng miễn phí an ninh không tốt, tôi cần phòng gửi xe có phí",
        facts=facts,
        previous_questions=["Phòng có hỗ trợ chỗ gửi xe máy không?"],
        generative_client=DisabledGenerativeClient(),
    )
    assert "Phòng B" in answer
    assert "80.000 đ/xe/tháng" in answer
    assert "Phòng A" not in answer
    assert "ưu tiên gửi xe không mất phí" not in answer
    assert "không đủ để kết luận nơi nào an ninh hơn" in answer


@pytest.mark.asyncio
async def test_paid_parking_request_never_recommends_free_room():
    facts = _room_facts()
    facts["listings"][0]["room"]["max_vehicles"] = 1
    facts["listings"][0]["charges"][0]["amount"] = 0
    facts["listings"][0]["charges"][0]["included_in_rent"] = True
    facts["listings"][1]["room"]["max_vehicles"] = 1
    for question in ("Tôi cần phòng gửi xe có thu phí", "Phòng nào gửi xe có phí?"):
        answer = await answer_listing_question(
            question=question, facts=facts, previous_questions=[],
            generative_client=DisabledGenerativeClient(),
        )
        assert "Phòng B" in answer and "Phòng A" not in answer


@pytest.mark.asyncio
async def test_free_parking_synonym_does_not_get_misread_as_paid():
    facts = _room_facts()
    facts["listings"][0]["room"]["max_vehicles"] = 1
    facts["listings"][0]["charges"][0]["amount"] = 0
    facts["listings"][0]["charges"][0]["included_in_rent"] = True
    answer = await answer_listing_question(
        question="Tôi muốn phòng gửi xe không mất phí", facts=facts,
        previous_questions=[], generative_client=DisabledGenerativeClient(),
    )
    assert "Phòng A" in answer and "Phòng B" not in answer


@pytest.mark.asyncio
async def test_parking_follow_up_answers_about_shown_listings_without_generic_template():
    result = {"total": 2, "matches": [
        {"id": "c3822603-ae07-e05c-a8f3-ec2da14e0352", "title": "Phòng A",
         "category": "ROOM", "price": 3_200_000, "parkingPolicy": "FREE", "maxVehicles": 9},
        {"id": "f199ecd2-5191-4d34-91c8-81a1f5a7e343", "title": "Phòng B",
         "category": "ROOM", "price": 3_200_000, "parkingPolicy": "PAID", "maxVehicles": 0},
    ]}
    reply = await compose_search_reply(
        question="Trong khu vực đó còn phòng nào có chỗ gửi xe không?",
        result_data=result,
        previous_ids=[item["id"] for item in result["matches"]],
        scoped=True,
    )
    assert "hai tin mình vừa gửi, có một tin ghi có chỗ gửi xe" in reply
    assert "Riêng tin “Phòng B”" in reply
    assert "tối đa 9 xe" in reply
    assert "chưa xác nhận chỗ gửi xe (chính sách thu phí nhưng tối đa 0 xe)" in reply
    assert "gửi xe miễn phí" in reply
    assert "gửi xe có thu phí" not in reply
    assert "Bạn muốn mình lọc tiếp" not in reply


class _HallucinatingClient(BaseGenerativeClient):
    async def generate_answer(self, question, context_chunks, *, audience="USER", mode="homespace"):
        return "Cả hai đều có chỗ gửi xe miễn phí và trả phí."


@pytest.mark.asyncio
async def test_initial_search_rejects_unasked_parking_claim():
    reply = await compose_search_reply(
        question="Tìm phòng trọ có gác và ban công dưới 3,5 triệu ở Bình Thạnh",
        result_data={"total": 1, "matches": [{
            "id": "c3822603-ae07-e05c-a8f3-ec2da14e0352", "title": "Phòng A",
            "category": "ROOM", "price": 3_200_000, "parkingPolicy": "FREE", "maxVehicles": 9,
        }]},
        previous_ids=[], scoped=False,
    )
    assert "gửi xe" not in reply
    assert "Phòng A" in reply


@pytest.mark.asyncio
async def test_initial_search_reply_is_warm_and_uses_actual_price_unit_and_area():
    reply = await compose_search_reply(
        question="Tìm phòng trọ ở Bình Thạnh", previous_ids=[], scoped=False,
        result_data={"total": 1, "matches": [{
            "id": "c3822603-ae07-e05c-a8f3-ec2da14e0352",
            "title": "Phòng A", "category": "ROOM", "price": 3_200_000,
            "priceUnit": "PERSON_MONTH", "areaM2": 28.0,
        }]},
    )
    assert "Mình tìm được một tin" in reply
    assert "3.200.000 đ/người/tháng" in reply
    assert "28 m²" in reply
    assert "Bạn muốn mình so sánh thêm" in reply


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_LIVE_LISTING_TESTS") != "1", reason="requires seeded homespace_core")
async def test_mcp_queries_live_published_listings():
    result = await call_listing_tool("search_listings", {
        "province_code": "79",
        "category": "ROOM",
        "price_max": 3_000_000,
        "has_mezzanine": True,
        "has_balcony": True,
    })
    assert result["total"] >= 0
    assert isinstance(result["listing_ids"], list)
    suggestions = await call_listing_tool("suggest_listing_places", {"province_code": "79"})
    assert isinstance(suggestions["suggestions"], list)
    fallback_suggestions = await call_listing_tool("suggest_listing_places", {
        "province_code": "01",
        "district": "Ba Đình",
    })
    assert fallback_suggestions["fallbackToProvince"] is False
    assert fallback_suggestions["suggestions"]
    ward = await call_listing_tool("resolve_listing_ward", {
        "province_code": "79",
        "query": "trọ gần đại học công nghiệp Gò Vấp",
    })
    assert ward["ward"] == "Phường Gò Vấp"
    details = await call_listing_tool("get_listing_facts", {"listing_ids": [
        "c3822603-ae07-e05c-a8f3-ec2da14e0352",
        "f199ecd2-5191-4d34-91c8-81a1f5a7e343",
        "f0cb3bc8-0b92-87cb-00ed-79963b48baa6",
        "6eb41796-4e76-fc57-7c9b-a7570aeb98fa",
    ]})
    by_id = {item["id"]: item for item in details["listings"]}
    room = by_id["f199ecd2-5191-4d34-91c8-81a1f5a7e343"]
    assert room["room"]["room_code"] == "P160"
    assert room["room"]["max_vehicles"] == 1
    assert room["owner"]["display_name"] == "System Administrator"
    assert any(c["charge_type"] == "MOTORBIKE_PARKING" and c["amount"] == 80000
               for c in room["charges"])
    parking_reply = await answer_listing_question(
        question=("Hai phòng vừa tìm, phòng nào gửi xe miễn phí, phòng nào thu phí? "
                  "Mỗi phòng để tối đa mấy xe?"),
        facts={"listings": [by_id["c3822603-ae07-e05c-a8f3-ec2da14e0352"], room]},
        previous_questions=[], generative_client=DisabledGenerativeClient(),
    )
    assert "P112" not in parking_reply  # Links use public titles, not internal room codes.
    assert "cửa sổ lớn" in parking_reply and "có gác sáng thoáng" in parking_reply
    assert "gửi xe miễn phí, tối đa 1 xe" in parking_reply
    assert "mức phí 80.000 đ/xe/tháng" in parking_reply
    apartment = by_id["f0cb3bc8-0b92-87cb-00ed-79963b48baa6"]
    assert apartment["apartment"]["project_name"] == "Sunrise City"
    assert apartment["apartment"]["floor_number"] == 9
    house = by_id["6eb41796-4e76-fc57-7c9b-a7570aeb98fa"]
    assert house["house"]["has_garage"] is True
    assert house["house"]["land_area_m2"] == 158
    assert "Cho thuê toàn bộ nhà" in house["house"]["rental_scope_description"]
    assert house["house"]["rented_floor_from"] == 1
    assert house["house"]["rented_floor_to"] == house["house"]["total_floors"] == 2
    with_garage = await call_listing_tool("search_listings", {
        "province_code": "79", "category": "HOUSE", "price_max": 10_000_000,
        "has_garage": True,
    })
    without_garage = await call_listing_tool("search_listings", {
        "province_code": "79", "category": "HOUSE", "price_max": 10_000_000,
        "has_garage": False,
    })
    assert with_garage["total"] == 4
    assert without_garage["total"] == 0
