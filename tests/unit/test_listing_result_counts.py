import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Request

from homespace_ai.application.listing_limits import requested_count, search_limit
from homespace_ai.application.listing_context import context_for, scoped_selection
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner, ToolPlan
from homespace_ai.tools.listings import ListingTools
from tests.unit.test_listing_ask import make_assistant, FakeTools
from tests.unit.test_listing_tool_planner import config, call

IDS = [f'00000000-0000-0000-0000-{n:012d}' for n in range(1, 101)]


@pytest.mark.parametrize('question,count', [
    ('Tìm 10 tin tại Gò Vấp',10), ('cho toi xem 10 phong tro',10), ('top 12 căn hộ rẻ nhất',12),
    ('Tìm mười tin đăng',10), ('Tìm hai mươi bài đăng',20), ('Xem 7 kết quả',7),
    ('Tìm phòng 2 phòng ngủ dưới 10 triệu cho 3 người',None), ('Xem chi tiết phòng 10',None),
    ('Tìm nhà 2 nhà vệ sinh, 3 phòng ngủ',None), ('Tìm mười lăm tin',15),
])
def test_extracts_result_quantity_not_a_price_bedroom_or_ordinal(question,count):
    assert requested_count(question) == count


@pytest.mark.asyncio
async def test_native_tool_schema_and_argument_use_requested_ten_not_default_five(monkeypatch):
    planner = ListingToolPlanner(config())
    async def provider(payload):
        assert payload['tools'][0]['parameters']['properties']['limit']['maximum'] == 20
        # Defend against a model that still returns its old default.
        return call('search_listings', {'filters':[], 'sort':'newest','limit':5})
    monkeypatch.setattr(planner, '_post', provider)
    result = await planner.plan('Tìm 10 tin tại HCM', [], {'category':'TEXT','city_id':'TEXT'})
    assert result.arguments['limit'] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize('question,available,expected', [
    ('Tìm phòng',100,5), ('Cho tôi xem 10 tin',100,10), ('Tìm 10 phòng trọ',3,3),
    ('Tìm 100 tin',100,20), ('Tìm 7 căn hộ',100,7),
])
async def test_count_reaches_query_cards_and_followup_context_without_another_hidden_slice(question,available,expected):
    assistant = make_assistant(ToolPlan('search_listings', {'filters':[], 'limit':5}))
    class Tools(FakeTools):
        def __init__(self):
            super().__init__(IDS[:available]); self.queries=[]; self.read=[]
        async def search(self, arguments):
            self.queries.append(arguments)
            return {'listingIds':self.ids[:arguments['limit']], 'total':available}
        async def detail(self, value):
            self.read.append(value); return await super().detail(value)
    assistant.tools = Tools()
    result = await assistant.try_answer(question, [], 'count')
    assert len(result.listing_ids) == len(assistant.tools.read) == expected
    assert result.listing_context == context_for(result.listing_ids)
    assert all(result.listing_positions[value] == n for n,value in enumerate(result.listing_ids,1))
    if '100' in question:
        assert 'tối đa 20' in result.answer
    if available == 3:
        assert 'chỉ có 3' in result.answer
    if expected == 10:
        history=[{'role':'assistant','listingContext':result.listing_context}]
        assert scoped_selection('Phòng 10 có gì?',history) == [IDS[9]]
        assert scoped_selection('So sánh 10 phòng này',history) == IDS[:10]


def test_server_cap_is_configurable_but_unspecified_quantity_stays_small():
    assert search_limit('Tìm 30 tin', SimpleNamespace(ai_listing_max_results=30)) == 30
    assert search_limit('Tìm phòng', SimpleNamespace(ai_listing_max_results=30)) == 5


@pytest.mark.asyncio
async def test_client_posts_limit_ten_and_preserves_ten_ids():
    gateway = AsyncMock()
    gateway.request.return_value = httpx.Response(200,json={'result':{'listingIds':IDS,'total':100}})
    tools=ListingTools(gateway,Request({'type':'http','headers':[]}))
    result=await tools.search({'filters':[], 'limit':10})
    assert gateway.request.call_args.kwargs['json']['limit'] == 10
    assert result['listingIds'] == IDS[:10]


@pytest.mark.asyncio
async def test_explicit_count_does_not_discard_a_question_about_parking_fees():
    assistant = make_assistant(ToolPlan('search_listings', {'filters':[]}))
    class Tools(FakeTools):
        async def search(self, arguments):
            return {'listingIds':self.ids[:arguments['limit']], 'total':100}
    assistant.tools = Tools(IDS)
    result = await assistant.try_answer('Tìm 10 tin, phí gửi xe bao nhiêu?', [], 'ten-with-fees')
    assert len(result.listing_ids) == 10
    assert '80.000' in result.answer
