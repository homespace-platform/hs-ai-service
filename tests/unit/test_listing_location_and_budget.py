import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Request

from homespace_ai.application.listing_evidence import compact_search_listing, MAX_SEARCH_LISTING_BYTES
from homespace_ai.application.listing_location import catalog, resolve_location_filters, LocationClarification
from homespace_ai.application.use_cases.listing_ask import LISTING_SIGNAL, ListingAsk
from homespace_ai.tools.listings import ListingTools
from tests.unit.listing_fakes import c, make_assistant, search

FIELDS = {name: 'TEXT' for name in ('category', 'city', 'city_id', 'ward', 'ward_id', 'street', 'price_amount', 'area_m2')}
FIELDS.update(price_amount='NUMBER', area_m2='NUMBER')


@pytest.mark.parametrize('city', ['HCM', 'TP.HCM', 'tphcm', 'Sài Gòn', 'Ho Chi Minh'])
def test_resolves_city_alias_to_the_actual_vietmap_id(city):
    result = resolve_location_filters([{'field':'city','op':'contains','value':city}], f'Tim phong tai {city}', [], FIELDS)
    assert result == [{'field':'city_id','op':'eq','value':'12'}]


def test_normalizes_accentless_ward_scoped_to_a_city_without_guessing_codes():
    result = resolve_location_filters([{'field':'ward','op':'contains','value':'go vap'},
        {'field':'city','op':'eq','value':'HCM'}], 'tim phong phuong go vap tai hcm', [], FIELDS)
    assert {'field':'ward_id','op':'eq','value':'24588'} in result
    assert {'field':'city_id','op':'eq','value':'12'} in result


def test_explicit_numeric_ward_survives_a_landmark_but_an_unmentioned_guess_does_not():
    filters = [{'field':'ward_id','op':'eq','value':'24588'}]
    assert filters[0] in resolve_location_filters(filters, 'Tim phong o phuong Go Vap gan IUH', [], FIELDS)
    assert not resolve_location_filters(filters, 'tim phong gan IUH', [], FIELDS)
    for question in ['Tim phong gan IUH, o phuong Go Vap HCM', 'Tim phong gan IUH o Phuong Go Vap']:
        assert filters[0] in resolve_location_filters(filters, question, [], FIELDS)


def test_legacy_district_and_unknown_or_ambiguous_locations_require_clarification():
    with pytest.raises(LocationClarification, match='quận/huyện cũ'):
        resolve_location_filters([{'field':'ward','op':'eq','value':'Gò Vấp'}], 'tim phong quan Go Vap', [], FIELDS)
    with pytest.raises(LocationClarification):
        resolve_location_filters([{'field':'ward_id','op':'eq','value':'999999'}], 'tim phong', [], FIELDS)
    cities, wards, _ = catalog()
    names = {}
    for code, (parent, name) in wards.items():
        names.setdefault(name, []).append(code)
    ambiguous = next(name for name, codes in names.items() if len(codes) > 1)
    with pytest.raises(LocationClarification, match='chưa xác định duy nhất'):
        resolve_location_filters([{'field':'ward','op':'eq','value':ambiguous}], f'Tìm phòng tại {ambiguous}', [], FIELDS)
    # An old district may be a parenthetical hint when a specific new ward was supplied.
    result = resolve_location_filters([{'field':'ward','op':'eq','value':'Phường An Nhơn'}],
        'tim phong Phuong An Nhon HCM (quan Go Vap cu)', [], FIELDS)
    assert {'field':'ward_id','op':'eq','value':'24332'} in result


def test_city_guess_inside_landmark_and_management_fee_are_not_location_requests():
    assert resolve_location_filters([], 'Tìm phòng gần ĐH Quốc gia TP.HCM', [], FIELDS) == []
    assert resolve_location_filters([], 'Tìm phòng phí quản lý thấp', [], FIELDS) == []


@pytest.mark.asyncio
async def test_every_user_predicate_reaches_the_core_query_before_limit():
    target = {'placeId': '11111111-1111-1111-1111-111111111111', 'name': 'Đại Học Công Nghiệp', 'address': ''}
    assistant = make_assistant(search(c('price_amount', 'lte', '5000000'), c('area_m2', 'gte', '20'),
                                      ward='Gò Vấp', city='HCM', place='IUH', keyword='IUH', limit=5, sort='price_asc'))
    assistant.tools.places = {'IUH': [target]}
    await assistant.try_answer('tim phong phuong go vap HCM gan IUH, duoi 5 trieu, tu 20 m2', [], 'all')
    payload = assistant.tools.payloads[0]
    for expected in [{'field': 'ward_id', 'op': 'eq', 'value': '24588'}, {'field': 'city_id', 'op': 'eq', 'value': '12'},
                     {'field': 'rent.monthly_amount', 'op': 'lt', 'value': '5000000'},
                     {'field': 'area_m2', 'op': 'gte', 'value': '20'}]:
        assert expected in payload['where']['all']
    assert payload['nearbyAll'][0]['placeId'] == target['placeId'] and 'keyword' not in payload
    assert payload['limit'] == 4 and payload['sort'] == 'rent_asc'


def test_packaged_snapshot_is_the_same_pair_as_frontend_and_contains_no_listings():
    root = Path(__file__).resolve().parents[2]
    data = json.loads((root / 'src/homespace_ai/data/vietmap/admin_new.json').read_text(encoding='utf-8'))
    assert len(data['cities']) == 34 and len(data['wards']) >= 3000
    assert data['ward_sha256'] and data['province_sha256']
    assert all(len(row) == 3 for row in data['wards'])


@pytest.mark.asyncio
async def test_accentless_fresh_message_reaches_listing_tools_and_skips_generation_for_browse():
    assert LISTING_SIGNAL.search('tim phong tro go vap duoi 5 trieu')
    assistant = make_assistant(search(ward='go vap', city='hcm'))
    result = await assistant.try_answer('tim phong tro phuong go vap hcm duoi 5 trieu', [], 'ascii')
    assert result.status == 'ANSWERED' and result.listing_ids
    assert assistant.planner.history == []
    assert {'field': 'rent.monthly_amount', 'op': 'lt', 'value': '5000000'} in assistant.tools.payloads[0]['where']['all']
    assert assistant.generative_client.kwargs is None


@pytest.mark.asyncio
async def test_ambiguous_location_is_a_question_without_search_or_rag():
    assistant = make_assistant(search(ward='Gò Vấp'))
    result = await assistant.try_answer('tim phong quan Go Vap', [], 'clarification')
    assert 'quận/huyện cũ' in result.answer and result.status == 'ANSWERED'
    assert assistant.tools.search_calls == 0
    assert result.listing_context['searchState']['pending']['type'] == 'LOCATION'


def test_search_evidence_is_bounded_even_with_huge_text_and_arrays_but_keeps_known_pricing():
    item = {'id':'00000000-0000-0000-0000-000000000001', 'title':'A' * 100000, 'category':'ROOM',
            'description':'private-long-description' * 100000,
            'pricing':{'amount':3500000,'unit':'ROOM_MONTH'},
            'roomDetail':{'maxOccupants':3,'hasWindow':True,'notes':'N' * 100000},
            'address':{'ward':'Phường Gò Vấp','display':'D' * 100000},
            'furnishings':[{'itemCode':'BED','assetName':'N' * 500,'quantity':1}] * 1000,
            'charges':[{'chargeType':'WATER','amount':5000,'unit':'m3','includedInRent':False}] * 1000}
    result = compact_search_listing(item)
    assert len(json.dumps(result, ensure_ascii=False).encode('utf-8')) <= MAX_SEARCH_LISTING_BYTES
    assert result['pricing']['amount'] == 3500000
    assert result['roomDetail']['maxOccupants'] == 3
    assert result['evidenceMode'] == 'search_preview' and result['omittedFields']
    assert 'description' not in result
    assert len(item['furnishings']) == 1000, 'Do not mutate full detail evidence'


@pytest.mark.asyncio
async def test_thousand_matches_never_cause_more_than_four_detail_reads():
    ids = [f'00000000-0000-0000-0000-{number:012d}' for number in range(1, 1001)]
    assistant = make_assistant(search(c('price_amount', 'lte', '5000000'), ward='Gò Vấp', city='HCM', limit=1000), ids=ids)
    result = await assistant.try_answer('tim phong phuong go vap hcm duoi 5 trieu', [], '1000')
    assert assistant.tools.payloads[0]['limit'] == 4
    assert len(assistant.tools.read) == len(result.listing_ids) == 4


@pytest.mark.asyncio
async def test_tool_clamps_outgoing_limit_and_incoming_ids_and_drops_upstream_extra_payload():
    ids = [f'00000000-0000-0000-0000-{number:012d}' for number in range(1, 1001)]
    gateway = AsyncMock()
    gateway.request.return_value = httpx.Response(200, json={'result':{'listingIds':ids,'total':1000,'readAt':'now',
        'listings':[{'description':'never forward this'}] * 1000}})
    tools = ListingTools(gateway, Request({'type':'http','headers':[]}))
    filters = [{'field':'ward_id','op':'eq','value':'24588'},{'field':'price_amount','op':'lte','value':'5000000'}]
    result = await tools.search({'filters':filters,'limit':1000})
    assert gateway.request.call_args.kwargs['json'] == {'filters':filters,'sort':'newest','limit':20}
    assert len(result['listingIds']) == 20 and result['total'] == 1000 and 'listings' not in result


@pytest.mark.asyncio
async def test_empty_search_is_one_query_with_every_hard_filter_and_no_hidden_alternative():
    amenities = ['ELEVATOR', 'WASHING_MACHINE', 'AIR_CONDITIONER']
    assistant = make_assistant(search(c('price_amount', 'lte', '5000000'), *[c(f'amenity.{n}', 'eq', 'true') for n in amenities],
                                      ward='Gò Vấp', city='HCM'), ids=[])
    result = await assistant.try_answer('tim phong phuong go vap hcm toi da 5 trieu', [], 'no-relax')
    assert result.status == 'NO_RESULTS' and assistant.tools.search_calls == 1
    where = assistant.tools.payloads[0]['where']['all']
    assert {'field': 'rent.monthly_amount', 'op': 'lte', 'value': '5000000'} in where
    assert all({'field': f'amenity.{n}', 'op': 'eq', 'value': 'true'} in where for n in amenities)
    assert 'không tự nới' in result.answer and '/rent/' not in result.answer
