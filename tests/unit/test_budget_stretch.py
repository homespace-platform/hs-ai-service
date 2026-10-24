from decimal import Decimal

from homespace_ai.application import budget_stretch as stretch
from homespace_ai.application.use_cases.listing_ask import ListingAsk
from homespace_ai.application.use_cases.ask_use_cases import AskResult


def room(rid, price, area):
    return {"id": rid, "areaM2": area, "pricing": {"amount": price}}


def test_tolerance_is_proportional_and_capped_at_500k():
    assert stretch.tolerance(Decimal(4_000_000)) == Decimal(300_000)
    assert stretch.tolerance(Decimal(20_000_000)) == Decimal(500_000)
    assert stretch.tolerance(Decimal(1_000_000)) == Decimal(100_000) or stretch.tolerance(Decimal(1_000_000)) == Decimal(50_000)


def test_budget_comes_from_the_upper_price_bound_only():
    filters = [{"field": "price_amount", "op": "lte", "value": "4000000"}, {"field": "area_m2", "op": "gte", "value": "30"}]
    assert stretch.budget_of(filters) == Decimal(4_000_000)
    assert stretch.budget_of([{"field": "price_amount", "op": "gte", "value": "4000000"}]) is None
    widened = stretch.stretch_filters(filters, Decimal(4_000_000))
    assert {"field": "area_m2", "op": "gte", "value": "30"} in widened
    assert {"field": "price_amount", "op": "gt", "value": "4000000"} in widened
    assert {"field": "price_amount", "op": "lte", "value": "4300000"} in widened


def test_standout_must_be_in_range_and_clearly_larger():
    main = [room("a", 3_900_000, 30), room("b", 4_000_000, 32)]
    cands = [room("c", 4_300_000, 50), room("d", 4_600_000, 70), room("e", 4_200_000, 33)]
    assert stretch.pick_standout(cands, main, Decimal(4_000_000))["id"] == "c"
    assert stretch.pick_standout([room("e", 4_200_000, 33)], main, Decimal(4_000_000)) is None
    note = stretch.stretch_note(cands[0], main, Decimal(4_000_000), 3)
    assert "Tin 3" in note and "300.000 đ" in note and "50 m²" in note and "32 m²" in note


def test_with_stretch_appends_one_card_and_note():
    a, b, c = (f"00000000-0000-0000-0000-00000000000{n}" for n in (1, 2, 3))
    result = AskResult(answer="Có 2 phòng.", status="ANSWERED", citations=[], request_id="r",
                       listing_ids=[a, b], listing_context={"listingIds": [a, b]})
    found = {"listing": room(c, 4_300_000, 50), "budget": Decimal(4_000_000),
             "inBudget": [room(a, 3_900_000, 30), room(b, 4_000_000, 32)]}
    out = ListingAsk._with_stretch(result, found)
    assert out.listing_ids == [a, b, c]
    assert out.listing_context["listingIds"] == [a, b, c]
    assert "ngoài tiêu chí" in out.answer
    assert ListingAsk._with_stretch(result, None) is result


def test_without_a_standout_the_answer_says_so_and_adds_no_card():
    result = AskResult(answer="Có 1 phòng.", status="ANSWERED", citations=[], request_id="r",
                       listing_ids=["00000000-0000-0000-0000-000000000001"], listing_context=None)
    out = ListingAsk._with_stretch(result, {"listing": None, "budget": Decimal(5_000_000), "inBudget": []})
    assert len(out.listing_ids) == 1 and "5.400.000 đ" in out.answer


def test_without_a_standout_the_answer_says_so_and_adds_no_card():
    result = AskResult(answer="Có 1 phòng.", status="ANSWERED", citations=[], request_id="r",
                       listing_ids=["00000000-0000-0000-0000-000000000001"], listing_context=None)
    out = ListingAsk._with_stretch(result, {"listing": None, "budget": Decimal(5_000_000), "inBudget": []})
    assert len(out.listing_ids) == 1 and "5.400.000 đ" in out.answer
