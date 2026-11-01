"""Catalog-wide location contracts and malformed native-tool plans, not just one happy path."""
from datetime import date

import pytest

from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.listing_location import catalog, resolve_location_filters, location_proposal, LocationClarification
from homespace_ai.application.listing_catalog import SearchCatalog
from homespace_ai.application.renter_criteria import normalize
from homespace_ai.clients.listing_tool_planner import ToolPlan
from tests.unit.listing_fakes import CATALOG, IDS, Conversation, make_assistant, search, leaves

FIELDS = {'ward_id': 'TEXT', 'city_id': 'TEXT'}


@pytest.mark.parametrize('proposed', [
    {'ward': 'Phường An Nhơn, Thành phố Hồ Chí Minh', 'city': None},
    {'ward': 'An Nhơn', 'city': 'Gia Lai'},
    {'ward': 'An Nhơn, HCM', 'city': 'HCM'},
    {'ward': 'Phường An Nhơn', 'city': 'Thành phố Hồ Chí Minh'},
    {},
])
def test_customer_explicit_pair_overrides_merged_wrong_or_missing_model_location_slots(proposed):
    q = 'phường An Nhơn, thành phố Hồ Chí Minh'
    update = normalize(q, search(**proposed).arguments, SearchCatalog(CATALOG), None, date(2026, 11, 1))
    assert update['location'] == {'ward': 'Phường An Nhơn', 'city': 'Thành phố Hồ Chí Minh'}
    filters = [{'field': slot, 'op': 'eq', 'value': value} for slot, value in update['location'].items()]
    assert resolve_location_filters(filters, q, [], FIELDS) == [
        {'field': 'ward_id', 'op': 'eq', 'value': '24332'}, {'field': 'city_id', 'op': 'eq', 'value': '12'}]


def test_every_vietmap_ward_city_pair_round_trips_without_substring_confusion():
    cities, wards, _ = catalog()
    problems = []
    for code, (city, ward) in wards.items():
        q = f'{ward}, {cities[city]}'
        try:
            result = resolve_location_filters([], q, [], FIELDS)
            if {x['field']: x['value'] for x in result} != {'ward_id': code, 'city_id': city}:
                problems.append((code, q, result))
        except LocationClarification as error:
            problems.append((code, q, str(error)))
    assert not problems, problems[:15]


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['TP.HCM nhé', 'Thành phố Hồ Chí Minh', 'ở HCM', 'phường An Nhơn, thành phố Hồ Chí Minh'])
@pytest.mark.parametrize('clarify_tool', [False, True])
async def test_ambiguous_ward_then_city_reply_keeps_pending_ward_budget_and_people(reply, clarify_tool):
    assistant = make_assistant(search(ward='An Nhơn'), IDS[:4])
    assistant._require_search_basics = True
    chat = Conversation(assistant)
    first = await chat.ask('Tìm phòng trọ phường An Nhơn cho 2 người dưới 5 triệu')
    assert assistant.tools.search_calls == 0
    assert 'chưa xác định duy nhất' in first.answer
    assert rs.sanitize(chat.state())['pending']['locationQuery'] == {'ward': 'Phường An Nhơn'}
    assistant.planner.plan_result = (ToolPlan('ask_clarification', {'missing': ['LOCATION'], 'question': 'Bạn muốn ở đâu?', 'options': []})
                                   if clarify_tool else search(ward='Phường An Nhơn, Thành phố Hồ Chí Minh'))
    second = await chat.ask(reply)
    assert second.status == 'ANSWERED' and assistant.tools.search_calls == 1, second.answer
    found = leaves(assistant.tools.payloads[-1])
    for item in [dict(field='ward_id', op='eq', value='24332'), dict(field='city_id', op='eq', value='12'),
                 dict(field='rent.monthly_amount', op='lt', value='5000000')]:
        assert item in found
    assert assistant.tools.payloads[-1]['context']['occupants'] == 2


def test_pending_location_can_change_province_or_be_reset_without_leaking_previous_choice():
    state = rs.empty_state()
    state['pending'] = {'type': 'LOCATION', 'locationQuery': {'ward': 'Phường An Nhơn'}}
    assert location_proposal('ở Gia Lai nhé', {}, state) == {'ward': 'Phường An Nhơn', 'city': 'Tỉnh Gia Lai'}
    assert location_proposal('tìm lại từ đầu ở Hà Nội', {}, state) == {'city': 'Thành phố Hà Nội'}


def test_new_ward_within_confirmed_city_uses_that_city_not_an_arbitrary_historical_province():
    state = rs.empty_state()
    state['location'] = {'cityId': '12', 'city': 'Thành phố Hồ Chí Minh', 'wardId': '24588', 'ward': 'Phường Gò Vấp'}
    assert location_proposal('Chuyển sang phường An Nhơn', {}, state) == {
        'ward': 'Phường An Nhơn', 'city': 'Thành phố Hồ Chí Minh'}


def test_street_and_school_names_do_not_become_administrative_restrictions():
    assert resolve_location_filters([], 'Tìm phòng gần Đại học Quốc gia TP.HCM', [], FIELDS) == []
    assert resolve_location_filters([], 'Tìm phòng đường An Nhơn, HCM', [], FIELDS) == [dict(field='city_id', op='eq', value='12')]


@pytest.mark.parametrize('ward,city', [('Xã Vân Canh', 'Tỉnh Gia Lai'), ('Xã Đường Hoa', 'Tỉnh Quảng Ninh'),
                                     ('Xã Thạnh An', 'Thành phố Hồ Chí Minh')])
def test_administrative_names_containing_can_h_or_duong_are_not_landmark_or_street_words(ward, city):
    found = resolve_location_filters([], f'Tìm phòng ở {ward}, {city}', [], FIELDS)
    assert any(item['field'] == 'ward_id' for item in found)


def test_an_explicit_invalid_pair_still_clarifies_instead_of_broadening_the_search():
    with pytest.raises(LocationClarification):
        resolve_location_filters([dict(field='ward', op='eq', value='Phường Gò Vấp')],
                                 'phường Gò Vấp, Hà Nội', [], FIELDS)


def test_legacy_pending_ward_recovers_from_full_customer_history_not_assistant_options():
    from homespace_ai.application.conversation_listing_memory import conversation_memory
    state = rs.empty_state()
    state['pending'] = {'type': 'LOCATION', 'question': 'Bạn muốn ở tỉnh nào?'}
    messages = [dict(role='user', content='Tìm phòng trọ phường An Nhơn cho 2 người dưới 5 triệu'),
                dict(role='assistant', content='Phường An Nhơn, Tỉnh Gia Lai; Phường An Nhơn, Thành phố Hồ Chí Minh')]
    messages += [dict(role='user', content='Mình chưa quyết định, mình cần có chỗ gửi xe')] * 10
    memory = conversation_memory({'messages': messages, 'renterSearchState': state})
    assert memory.state['pending']['locationQuery'] == {'ward': 'Phường An Nhơn'}
    assert len(memory.history) == 8 and memory.repaired
