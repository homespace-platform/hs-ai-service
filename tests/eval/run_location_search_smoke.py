"""Opt-in real planner + Gateway/Core read-only smoke. No conversation writes or TomTom calls.

HS_AI_LIVE_EVAL=1 uv run --no-sync python -m tests.eval.run_location_search_smoke --confirm-paid
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from fastapi import Request
from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.use_cases.listing_ask import ListingAsk
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.clients.generative import OpenAIGenerativeClient
from homespace_ai.core.config import get_settings


class ReadOnlyGateway(GatewayApiClient):
    def __init__(self, *args):
        super().__init__(*args)
        self.searches = []

    async def request(self, method, path, **kwargs):
        assert path.startswith('/api/v1/public/listings/ai'), 'Smoke only reads public listing tools'
        assert method == 'GET' or (method == 'POST' and path.rsplit('/', 1)[-1] in {'search', 'nearby-summary', 'value-opportunity'})
        if path.endswith('/search'):
            self.searches.append(kwargs.get('json'))
        return await super().request(method, path, **kwargs)


async def run():
    settings = get_settings()
    gateway = ReadOnlyGateway(str(settings.gateway_base_url), settings.gateway_request_timeout_seconds)
    assistant = ListingAsk(settings, OpenAIGenerativeClient(settings), gateway, Request({'type': 'http', 'headers': []}))
    rows = []
    try:
        for turns in [
            ['Tìm phòng trọ phường An Nhơn cho 2 người dưới 5 triệu', 'phường An Nhơn, thành phố Hồ Chí Minh'],
            ['Tìm phòng trọ phường An Nhơn cho 2 người dưới 5 triệu', 'TP.HCM nhé'],
            ['Tìm phòng trọ phường An Nhơn, HCM cho 2 người dưới 5 triệu', 'Đổi sang phường An Nhơn ở Gia Lai'],
        ]:
            history = []
            state = rs.empty_state()
            state['boundaryAdvice'] = 'DENY'  # Isolate location/criteria routing, not optional price advice.
            for question in turns:
                before = len(gateway.searches)
                result = await assistant.try_answer(question, history, 'location-smoke', state=state)
                assert result is not None
                state = result.listing_context['searchState']
                history += [dict(role='user', content=question), dict(role='assistant', content=result.answer,
                            status=result.status, listingContext=result.listing_context)]
                payloads = gateway.searches[before:]
                location = state.get('location') or {}
                ambiguous = question == turns[0] and 'HCM' not in question
                if ambiguous:
                    assert not payloads and (state.get('pending') or {}).get('type') == 'LOCATION'
                else:
                    assert len(payloads) == 1, (question, result.status, result.answer[:300])
                    assert location.get('cityId') == ('25' if 'Gia Lai' in question else '12'), location
                    assert location.get('wardId'), location
                    assert payloads[0]['context']['occupants'] == 2
                    assert any(c['field'] == 'rent.monthly_amount' and c['op'] == 'lt' and str(c['value']) == '5000000'
                               for c in payloads[0]['where']['all'])
                rows.append(dict(question=question, status=result.status, location=location,
                                 payload=payloads[0] if payloads else None,
                                 displayedCount=len(result.listing_context.get('listingIds') or []), passed=True))
    finally:
        await gateway.aclose()
    report = dict(model=settings.openai_tool_model, tomtomRequests=0, conversationWrites=0, cases=rows)
    target = Path(__file__).parent / 'results' / 'location_search_smoke_20261101.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(passed=len(rows), report=str(target)), ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--confirm-paid', action='store_true')
    args = parser.parse_args()
    if os.environ.get('HS_AI_LIVE_EVAL') != '1' or not args.confirm_paid:
        parser.error('Set HS_AI_LIVE_EVAL=1 and --confirm-paid to use the configured model')
    asyncio.run(run())


if __name__ == '__main__':
    main()
