"""Opt-in real-model smoke with synthetic F4 facts; no Core, Mongo or maps traffic.

uv run python scripts/smoke_conversation_quality.py --live-model --output <report.json>
The ordinary test suite remains fully offline. Never use production conversation IDs here.
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homespace_ai.core.config import get_settings
from homespace_ai.clients.generative import get_generative_client
from tests.unit.test_conversation_quality import prepared


async def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    if settings.generation_provider != "openai":
        raise SystemExit("NOT_RUN: the configured real model is unavailable.")
    generator = get_generative_client(settings)
    report = {"startedAt": datetime.now(timezone.utc).isoformat(), "model": settings.openai_listing_model,
              "fixture": "synthetic F4, no application or customer data", "tomtomRequests": 0, "cases": []}
    for q in ["Trong các phòng này, các khoản phí hằng tháng khác nhau thế nào?",
              "Bạn so giúp các khoản phí phải trả thêm mỗi tháng của mấy phòng này.",
              "Phòng nào đỡ tốn tiền hơn nếu tính cả các khoản phí?"]:
        a, chat = await prepared()
        a.generative_client = generator
        before = a.tools.search_calls, len(a.tools.value_calls)
        answer = await chat.ask(q)
        scope = answer.listing_context or {}
        assert answer.status == "ANSWERED" and "| Tiêu chí |" in answer.answer
        assert answer.answer_context["intent"] == "COST"
        assert (a.tools.search_calls, len(a.tools.value_calls)) == before
        assert not a.tools.nearby_calls
        # Free parking alone cannot identify one of the three equal peers. It may be
        # mentioned alongside a genuinely distinguishing rent/cost advantage.
        assert (scope.get("mainRecommendation") or {}).get("fields") != ["charges.MOTORBIKE_PARKING.includedInRent"]
        row = {"question": q, "focus": answer.answer_context["intent"], "answer": answer.answer,
               "choice": scope.get("mainRecommendation"), "priorities": scope.get("priorityRecommendations"),
               "suggestedQuestions": answer.suggested_questions}
        report["cases"].append(row)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if scope.get("mainRecommendation"):
            why = await chat.ask("Vì sao bạn chọn phương án tiết kiệm?")
            row["why"] = why.answer
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            assert "| Tiêu chí đối chiếu |" in why.answer
            assert why.listing_context["mainRecommendation"]["listingId"] == scope["mainRecommendation"]["listingId"]
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"focus": row["focus"], "status": "PASS", "hasChoice": bool(row["choice"])}), flush=True)
    print(json.dumps({"cases": len(report["cases"]), "tomtomRequests": 0, "report": str(args.output)}))


if __name__ == "__main__":
    asyncio.run(run())
