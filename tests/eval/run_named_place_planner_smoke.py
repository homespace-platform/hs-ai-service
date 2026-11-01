"""Opt-in real model routing; fixture catalog, no DB writes and no TomTom calls.

HS_AI_LIVE_EVAL=1 uv run --no-sync python -m tests.eval.run_named_place_planner_smoke --confirm-paid
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from datetime import date
from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.listing_catalog import SearchCatalog
from homespace_ai.application.renter_criteria import normalize
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner
from homespace_ai.core.config import get_settings
from tests.unit.listing_fakes import CATALOG, IDS


async def run():
    settings=get_settings()
    planner=ListingToolPlanner(settings)
    catalog=SearchCatalog(CATALOG)
    rows=[]
    for question in [
        "Tôi cần tìm phòng trọ ở gần Đại học Công Nghiệp Hồ Chí Minh",
        "Tìm phòng trọ gần Đại học công nghiệp HCM cho 2 người dưới 4 triệu, cho tôi xem 4 phòng",
        "Tìm phòng trọ gần Trường Đại Học Công Nghiệp Thành Phố Hồ Chí Minh — 12 Đường Nguyễn Văn Bảo, Hạnh Thông, Hồ Chí Minh cho 3 người dưới 5 triệu",
    ]:
        plan=await planner.plan(question,[],catalog,None,date.today())
        assert plan and plan.name=="search_listings",(question,plan)
        update=normalize(question,plan.arguments,catalog,None,date.today())
        assert update.get("placeQuery"),(question,plan.arguments)
        assert not plan.arguments.get("withinDisplayed")
        rows.append(dict(question=question,tool=plan.name,placeQuery=update["placeQuery"],passed=True))
    state=rs.merge(None,{"constraints":[{"field":"category","op":"eq","value":"ROOM","strength":"hard"}],
        "target":{"placeId":IDS[9],"name":"Địa điểm khách đã chọn","address":"12 Nguyễn Văn Bảo","radiusMeters":2000},
        "context":{"occupants":2}},"")
    plan=await planner.plan("Ngân sách dưới 4 triệu nhé",[],catalog,state,date.today())
    assert plan and plan.name=="search_listings"
    update=normalize("Ngân sách dưới 4 triệu nhé",plan.arguments,catalog,state,date.today())
    assert rs.merge(state,update,"Ngân sách dưới 4 triệu nhé")["target"]==state["target"]
    rows.append(dict(question="Ngân sách dưới 4 triệu nhé",tool=plan.name,preservedTarget=True,passed=True))
    report=dict(model=settings.openai_tool_model,tomtomRequests=0,conversationWrites=0,cases=rows)
    path=Path(__file__).parent/"results"/"named_place_planner_smoke_20261102.json"
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(dict(passed=len(rows),report=str(path)),ensure_ascii=True))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--confirm-paid",action="store_true")
    args=parser.parse_args()
    if os.environ.get("HS_AI_LIVE_EVAL")!="1" or not args.confirm_paid:
        parser.error("Set HS_AI_LIVE_EVAL=1 and --confirm-paid to use the configured model")
    asyncio.run(run())


if __name__=="__main__":
    main()
