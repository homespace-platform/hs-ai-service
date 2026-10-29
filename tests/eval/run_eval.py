"""Write per-ID corpus results.

  uv run --no-sync python -m tests.eval.run_eval offline
      Deterministic text guards only; no network. Live status stays NOT_RUN.
  HS_AI_LIVE_EVAL=1 uv run --no-sync python -m tests.eval.run_eval live --confirm-paid [--ids Q001,Q002]
      Paid: calls the configured OpenAI planner (key read by Settings from the service .env, never printed) for
      single-turn cases without setup. Core, TomTom and the database are not called; multi-turn cases and cases
      that need displayed results stay NOT_RUN and must be run through the chat UI.
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

from tests.eval.corpus import CORPUS, ROOT, catalog, check_guard, compare, load, observed

RESULTS = ROOT / "results"
TOOL_OUTCOME = {"search_listings": "SEARCH", "ask_clarification": "CLARIFY", "get_listing_detail": "DETAIL",
                "get_listing_details": "COMPARE", "get_listing_nearby_places": "NEARBY_EVIDENCE"}


def _accepted(expected: str) -> set[str]:
    return {part.split("_WITH")[0].split("_THEN")[0] for part in expected.split("_OR_")}


def offline() -> dict[str, Any]:
    from homespace_ai.application.renter_dates import request_today
    from tests.unit.listing_fakes import FIXED_NOW

    corpus, cat = load(), catalog()
    today = request_today(lambda: FIXED_NOW, timedelta(hours=7))
    rows = []
    for case in corpus["cases"]:
        problems = check_guard(case, cat, today) if case.get("guard") else None
        rows.append({"id": case["id"], "offlineGuard": "NOT_APPLICABLE" if problems is None else ("PASS" if not problems else "FAIL"),
                     "problems": problems or [], "live": case["status"]})
    return {"mode": "offline", "fixedToday": str(today), "cases": rows,
            "summary": {k: sum(r["offlineGuard"] == k for r in rows) for k in ("PASS", "FAIL", "NOT_APPLICABLE")}}


async def live(ids: set[str] | None) -> dict[str, Any]:
    from homespace_ai.clients.listing_tool_planner import ListingToolPlanner
    from homespace_ai.core.config import Settings

    settings = Settings()
    planner, cat = ListingToolPlanner(settings), catalog()
    today = date.fromisoformat(load()["fixedToday"])
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for case in load()["cases"]:
        if ids and case["id"] not in ids:
            continue
        if case["id"].startswith("C") or case.get("setup"):
            rows.append({"id": case["id"], "live": "NOT_RUN", "reason": "needs conversation state or displayed results; run in chat UI"})
            continue
        try:
            plan = await planner.plan(case["input"], [], cat, None, today)
        except Exception as error:  # noqa: BLE001 - recorded per case, the message never contains the key
            rows.append({"id": case["id"], "live": "FAIL", "reason": type(error).__name__})
            continue
        outcome = TOOL_OUTCOME.get(plan.name if plan else "", "NONE")
        problems = [] if outcome in _accepted(case["expect"]["outcome"]) else [f"outcome {outcome} != {case['expect']['outcome']}"]
        if plan and plan.name == "search_listings":
            seen = observed(case["input"], cat, today, plan.arguments)
            problems += compare({k: case["expect"].get(k) for k in ("required", "forbidden", "preferences")}, seen)
        rows.append({"id": case["id"], "live": "PASS" if not problems else "FAIL", "tool": plan.name if plan else None,
                     "arguments": plan.arguments if plan else None, "problems": problems})
    return {"mode": "live-planner", "model": settings.openai_tool_model, "requestedAt": started,
            "catalogVersion": cat.catalog_version, "corpusSha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
            "fixedToday": str(today), "cases": rows,
            "summary": {k: sum(r["live"] == k for r in rows) for k in ("PASS", "FAIL", "NOT_RUN")}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["offline", "live"])
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument("--ids")
    args = parser.parse_args()
    RESULTS.mkdir(exist_ok=True)
    if args.mode == "offline":
        report, target = offline(), RESULTS / "offline_guard_results.json"
    else:
        if os.environ.get("HS_AI_LIVE_EVAL") != "1" or not args.confirm_paid:
            print("Live eval is paid and opt-in: set HS_AI_LIVE_EVAL=1 and pass --confirm-paid.", file=sys.stderr)
            return 2
        report = asyncio.run(live(set(args.ids.split(",")) if args.ids else None))
        target = RESULTS / "live_planner_results.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{target.name}: {report['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
