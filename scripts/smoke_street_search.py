"""Opt-in real planner + public Core search smoke; no TomTom, writes or conversation persistence."""
import argparse
import asyncio
from datetime import date
import json
from pathlib import Path
from decimal import Decimal

import httpx

from homespace_ai.application import renter_search_state as rs
from homespace_ai.application.listing_catalog import SearchCatalog
from homespace_ai.application.renter_criteria import normalize
from homespace_ai.application.renter_guidance import missing_basics
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner
from homespace_ai.core.config import get_settings


async def main(args):
    report = {"tomtomRequests": 0, "cases": []}
    settings = get_settings()
    planner = ListingToolPlanner(settings)
    async with httpx.AsyncClient(base_url=args.core, timeout=30) as client:
        response = await client.get("/api/v1/public/listings/ai/catalog")
        response.raise_for_status()
        catalog = SearchCatalog(response.json()["result"])
        state = None
        history = []
        for question in [
            "Tìm phòng trọ đường Phan Văn Trị cho 3 người giá dưới 4 triệu",
            "Ở TP.HCM nhé",
            "Tăng giá lên dưới 5 triệu, vẫn cho 3 người",
            "Cho 2 người thôi, giá dưới 4 triệu",
        ]:
            plan = await planner.plan(question, history, catalog, state, date.today())
            assert plan is not None and plan.name in {"search_listings", "ask_clarification"}, "Wrong tool"
            proposed = plan.arguments if plan.name == "search_listings" else {"constraints": []}
            update = normalize(question, proposed, catalog, state, date.today())
            state = rs.merge(state, update, question)
            if update.get("location"):
                from homespace_ai.application.use_cases.listing_ask import ListingAsk
                state["location"] = ListingAsk._resolve_location(None, update["location"], question, history)
            missing = missing_basics(state)
            payload = rs.core_request(state, limit=4)
            row = {"question": question, "tool": plan.name, "missing": missing, "payload": payload}
            assert any(c.get("field") == "street" and c["value"] == "Phan Văn Trị" for c in payload["where"]["all"])
            assert not any(c.get("field") == "ward_id" for c in payload["where"]["all"])
            if missing:
                assert missing == ["LOCATION"]
                state["pending"] = {"type": "INTAKE", "question": "Bạn muốn tìm đường này ở tỉnh/thành nào?", "fields": missing,
                                    "candidates": [], "radiusMeters": None, "proposed": None}
            else:
                response = await client.post("/api/v1/public/listings/ai/search", json=payload)
                response.raise_for_status()
                found = response.json()["result"]
                row["total"] = found["total"]
                row["returned"] = len(found["listingIds"])
                assert row["total"] > 0, "Expected seed matches on Phan Van Tri"
                ceiling = next(c["value"] for c in payload["where"]["all"] if c.get("field") == "rent.monthly_amount" and c["op"] == "lt")
                for item_id in found["listingIds"]:
                    detail = await client.get(f"/api/v1/public/listings/ai/{item_id}")
                    detail.raise_for_status()
                    item = detail.json()["result"]
                    assert "Phan Văn Trị" in item["address"]["street"]
                    assert item["category"] == "ROOM"
                    assert item["roomDetail"]["maxOccupants"] >= state["context"]["occupants"]
                    assert item["pricing"]["unit"] == "ROOM_MONTH" and Decimal(str(item["pricing"]["amount"])) < Decimal(str(ceiling))
            report["cases"].append(row)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            history += [{"role": "user", "content": question},
                        {"role": "assistant", "content": "Mình đã giữ các điều kiện.",
                         "listingContext": {"listingIds": [], "searchState": state}}]
            print(json.dumps({"tool": row["tool"], "missing": missing, "total": row.get("total"), "status": "PASS"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", default="http://localhost:8080")
    parser.add_argument("--output", type=Path, default=Path("tests/eval/results/street_search_smoke.json"))
    asyncio.run(main(parser.parse_args()))
