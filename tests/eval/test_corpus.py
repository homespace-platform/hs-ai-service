"""Offline checks of the 200-turn renter corpus. No model, Core or provider is called."""
import re

import pytest

from tests.eval import run_eval
from tests.eval.corpus import (PSEUDO_FIELDS, SOURCE, STATUSES, catalog, check_guard, compare, field_codes, load, observed,
                               source_inputs)
from tests.unit.listing_fakes import FIXED_NOW
from homespace_ai.application.renter_dates import request_today
from datetime import timedelta

CORPUS = load()
CASES = CORPUS["cases"]
CAT = catalog()
TODAY = request_today(lambda: FIXED_NOW, timedelta(hours=7))


def test_corpus_has_all_200_turns_with_original_ids():
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids)) == 200
    assert [i for i in ids if i.startswith("Q")] == [f"Q{n:03d}" for n in range(1, 121)]
    assert [i for i in ids if i.startswith("C")] == [f"C{c:02d}.{t}" for c in range(1, 11) for t in range(1, 9)]
    for case in CASES:
        if case["id"].startswith("C"):
            assert case["conversation"] == case["id"][:3] and case["turn"] == int(case["id"][-1])
        assert case["input"].strip() and case["status"] in STATUSES and case["expect"]["outcome"]


def test_no_live_result_is_claimed_without_a_run():
    assert {case["status"] for case in CASES} == {"NOT_RUN"}


@pytest.mark.skipif(not SOURCE.exists(), reason="manual acceptance document is outside this checkout")
def test_inputs_are_the_customer_text_of_the_source_document():
    source = source_inputs()
    assert len(source) == 200
    for case in CASES:
        assert case["input"] == source[case["id"]], case["id"]
    setup = "Cho mình xem 10 chỗ thuê ở An Nhơn, Hồ Chí Minh, tiền thuê không quá 5 triệu."
    assert setup in SOURCE.read_text(encoding="utf-8")
    assert all(case.get("setup") == [setup] for case in CASES if re.fullmatch(r"Q1(0[6-9]|1[0-5])", case["id"]))


def test_expected_fields_and_kinds_exist_in_the_core_catalog():
    codes = set(CAT.fields) | PSEUDO_FIELDS
    kinds = {k["code"] for k in CAT.kinds}
    for case in CASES:
        for code in field_codes(case):
            assert code in codes or code.startswith("context."), f"{case['id']}: unknown field {code}"
        for spec in case["expect"].get("required") or []:
            if spec[0] == "nearby.kind":
                assert spec[2] in kinds, case["id"]
    unsupported = {u["code"] for u in CAT.unsupported}
    assert {"QUIETNESS", "TRAVEL_TIME", "SECURITY_QUALITY"} <= unsupported


GUARDED = [case for case in CASES if case.get("guard")]


@pytest.mark.parametrize("case", GUARDED, ids=[case["id"] for case in GUARDED])
def test_offline_text_guard(case):
    assert not check_guard(case, CAT, TODAY)


def test_offline_report_covers_every_id_and_keeps_live_not_run():
    report = run_eval.offline()
    assert [row["id"] for row in report["cases"]] == [case["id"] for case in CASES]
    assert report["summary"] == {"PASS": len(GUARDED), "FAIL": 0, "NOT_APPLICABLE": 200 - len(GUARDED)}
    assert {row["live"] for row in report["cases"]} == {"NOT_RUN"}


def test_live_eval_refuses_to_spend_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("HS_AI_LIVE_EVAL", raising=False)
    monkeypatch.setattr("sys.argv", ["run_eval", "live", "--confirm-paid"])
    assert run_eval.main() == 2
    monkeypatch.setenv("HS_AI_LIVE_EVAL", "1")
    monkeypatch.setattr("sys.argv", ["run_eval", "live"])
    assert run_eval.main() == 2


def test_live_comparison_uses_the_same_deterministic_layer_as_chat():
    from tests.unit.listing_fakes import c, search
    q006 = next(case for case in CASES if case["id"] == "Q006")
    expect = {k: q006["expect"].get(k) for k in ("required", "forbidden", "preferences")}
    good = search(c("room.restroom_type", "eq", "PRIVATE"), c("room.has_window", "eq", True), ward="Hạnh Thông", city="HCM")
    assert compare(expect, observed(q006["input"], CAT, TODAY, good.arguments)) == []
    lost = search(c("room.has_window", "eq", True), ward="Hạnh Thông", city="HCM")
    problems = compare(expect, observed(q006["input"], CAT, TODAY, lost.arguments))
    assert len(problems) == 1 and "room.restroom_type" in problems[0]


def test_field_coverage_matrix_is_current_and_lists_every_catalog_field():
    from tests.eval import field_matrix
    text = field_matrix.render()
    assert field_matrix.TARGET.read_text(encoding="utf-8") == text
    assert all(f"| `{code}` |" in text for code in CAT.fields)


def test_guard_coverage_is_reported_not_inflated():
    assert len(GUARDED) >= 40
    assert all(not case["id"].startswith("C") for case in GUARDED), "Multi-turn cases need the live runner, not a text guard"
