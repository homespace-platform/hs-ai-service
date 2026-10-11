"""Opt-in paid smoke tests with synthetic fixtures; no DB, Gateway or user data.

Run: uv run python scripts/smoke_openai.py --live
"""
import argparse
import asyncio
import json

from homespace_ai.clients.generative import GenerationUnavailableError, get_generative_client
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner
from homespace_ai.core.config import Settings


async def check() -> None:
    settings = Settings()
    if settings.generation_provider != "openai":
        raise SystemExit("Set GENERATION_PROVIDER=openai in the ignored .env.")
    generator = get_generative_client(settings)
    answer = await generator.generate_answer(
        "Trong tài liệu thử nghiệm, khách đặt lịch ở đâu?",
        [{"title": "Tài liệu thử nghiệm", "heading": "Đặt lịch",
          "content": "Khách chọn Đặt lịch xem nhà trên trang chi tiết tin đăng."}],
    )
    if not answer or "[C1]" not in answer:
        raise SystemExit("FAIL: RAG answer/citation check.")
    print("PASS: RAG generation and citation.")

    planner = ListingToolPlanner(settings)
    fields = {"category": "TEXT", "ward_name": "TEXT", "room.has_balcony": "BOOLEAN",
              "room.parking_policy": "TEXT"}
    plan = await planner.plan(
        "Tôi cần tìm phòng trọ tại Phường Phú Lợi có ban công, gửi xe có phí.", [], fields)
    expected = [
        {"field": "category", "op": "eq", "value": "ROOM"},
        {"field": "ward_name", "op": "eq", "value": "Phường Phú Lợi"},
        {"field": "room.has_balcony", "op": "eq", "value": "true"},
        {"field": "room.parking_policy", "op": "eq", "value": "PAID"},
    ]
    if plan is None or plan.name != "search_listings" or any(
        item not in plan.arguments["filters"] for item in expected
    ):
        raise SystemExit("FAIL: strict search tool arguments.")
    print("PASS: native tool calling and explicit paid-parking preference.")

    # Deliberately larger than a tiny chat prompt; passed intact, never compacted.
    evidence = {"listings": [
        {"id": f"00000000-0000-0000-0000-{index:012d}", "category": "ROOM",
         "title": f"Phòng thử nghiệm {index}", "roomDetail": {"parkingPolicy": "PAID"},
         "charges": [{"chargeType": "MOTORBIKE_PARKING", "amount": index * 10000,
                      "includedInRent": False}],
         "description": "Dữ liệu tổng hợp thử nghiệm, không phải tin đăng thật. " * 200}
        for index in range(1, 6)
    ]}
    answer = await generator.generate_answer(
        "So sánh phí gửi xe của năm tin thử nghiệm bằng bảng Markdown. Chỉ nêu phí gửi xe.",
        [{"content": json.dumps(evidence, ensure_ascii=False)}], mode="listing",
    )
    if "|" not in answer or not all(
        any(formatted in answer for formatted in (f"{index}0.000", f"{index}0,000"))
        for index in range(1, 6)
    ):
        raise SystemExit("FAIL: full-evidence listing comparison.")
    print("PASS: full-evidence listing generation and Markdown comparison table.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize three small paid API requests.")
    if not parser.parse_args().live:
        parser.error("--live is required; this test uses paid OpenAI requests.")
    try:
        asyncio.run(check())
    except GenerationUnavailableError as error:
        raise SystemExit(f"FAIL: {error}") from None
