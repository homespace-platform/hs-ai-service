import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from homespace_ai.api.v1 import rental_request_review
from homespace_ai.application import rental_review
from homespace_ai.clients.generative import GenerationUnavailableError
from homespace_ai.core.security import UserContext, get_current_user
from homespace_ai.main import app
from homespace_ai.tools.rental_requests import RentalRequestTools, RentalRequestToolError

LISTING = "00000000-0000-0000-0000-0000000000aa"


def req(rid, name, months, people, move_in="2026-11-01", deposit=3_000_000, created="2026-10-23T03:00:00Z", rent=3_000_000):
    return {"id": rid, "renterName": name, "leaseMonths": months, "occupantCount": people, "moveInDate": move_in,
            "depositAmount": deposit, "monthlyRentPrice": rent, "createdAt": created, "status": "PENDING",
            "listingTitle": "Phòng A"}


def run(*items):
    return rental_review.review({"listingId": LISTING, "listingTitle": "Phòng A"}, list(items))


def test_longer_lease_wins_when_everything_else_is_equal():
    result = run(req("a", "An", 3, 1), req("b", "Bình", 12, 1))
    assert result["recommendedRequestId"] == "b"
    assert result["ranking"][0]["score"] == 100 and result["ranking"][1]["score"] < 100
    assert any("dài nhất" in text for text in result["ranking"][0]["strengths"])
    assert any("ngắn nhất" in text for text in result["ranking"][1]["weaknesses"])
    assert "lease" in result["weights"] and "rent" not in result["weights"]  # equal criteria do not count


def test_fewer_occupants_wins_when_everything_else_is_equal():
    result = run(req("a", "An", 12, 3), req("b", "Bình", 12, 1))
    assert result["recommendedRequestId"] == "b"
    assert "ít người nhất" in " ".join(result["ranking"][0]["strengths"])


def test_weights_resolve_a_conflict_between_criteria():
    # A: 12 months but 4 people; B: 6 months, 1 person. Lease (35) outweighs occupants (20).
    result = run(req("a", "An", 12, 4), req("b", "Bình", 6, 1))
    assert result["recommendedRequestId"] == "a"
    assert result["margin"] == pytest.approx(27.3, abs=0.25)


def test_three_requests_are_ranked_and_marked_close_calls_when_scores_are_near():
    result = run(req("a", "An", 12, 2), req("b", "Bình", 12, 2), req("c", "Chi", 3, 4))
    assert [row["rank"] for row in result["ranking"]] == [1, 2, 3]
    assert result["ranking"][2]["requestId"] == "c"
    # Identical on every criterion: nothing separates them, which is reported as a close call.
    tie = run(req("a", "An", 12, 2), req("b", "Bình", 12, 2))
    assert tie["closeCall"] is True and "không khác nhau đáng kể" in tie["summary"]


def test_earlier_submission_breaks_a_tie():
    result = run(req("late", "Muộn", 12, 2, created="2026-10-23T05:00:00Z"),
                 req("early", "Sớm", 12, 2, created="2026-10-23T01:00:00Z"))
    assert result["recommendedRequestId"] == "early"


def test_missing_data_earns_no_points_and_needs_two_requests():
    missing = dict(req("a", "An", 12, 2), depositAmount=None)
    result = run(missing, req("b", "Bình", 12, 2, deposit=5_000_000))
    assert result["recommendedRequestId"] == "b"
    with pytest.raises(ValueError):
        run(req("only", "Một", 12, 1))


def test_summary_never_leaks_contact_data_or_internal_ids():
    item = dict(req("a", "An", 3, 1), renterPhone="0900000000", renterEmail="a@x.vn", renterId="user-1")
    text = run(item, req("b", "Bình", 12, 1))["summary"]
    assert "0900000000" not in text and "a@x.vn" not in text and "user-1" not in text
    assert "quyền quyết định" in text


@pytest.mark.asyncio
async def test_tool_reads_owner_pending_requests_and_drops_contact_fields():
    seen = {}

    class Gateway:
        async def request(self, method, path, **kwargs):
            seen.update(method=method, path=path, params=kwargs["params"])
            row = dict(req("a", "An", 3, 1), renterPhone="0900000000", renterEmail="a@x.vn", renterId="u", ownerId="o")
            return httpx.Response(200, json={"result": [row, dict(row, id="x", status="ACCEPTED")]})

    scope = {"type": "http", "headers": [(b"authorization", b"Bearer t")]}
    tool = RentalRequestTools(Gateway(), Request(scope))
    rows = await tool.pending_for_listing(LISTING)
    assert seen["path"] == "/api/v1/rental-requests/owner" and seen["params"]["status"] == "PENDING"
    assert [row["id"] for row in rows] == ["a"]
    assert not {"renterPhone", "renterEmail", "renterId", "ownerId"} & set(rows[0])
    with pytest.raises(RentalRequestToolError):
        await tool.pending_for_listing("not-a-uuid")


class FakeTools:
    rows: list = []
    error: RentalRequestToolError | None = None

    def __init__(self, gateway, request):
        pass

    async def pending_for_listing(self, listing_id):
        if FakeTools.error:
            raise FakeTools.error
        return FakeTools.rows


@pytest.fixture
def client(monkeypatch):
    FakeTools.rows, FakeTools.error = [req("a", "An", 3, 1), req("b", "Bình", 12, 1)], None
    monkeypatch.setattr(rental_request_review, "RentalRequestTools", FakeTools)
    app.dependency_overrides[get_current_user] = lambda: UserContext(**{"X-User-Id": "owner", "X-User-Role": "USER"})
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_review_endpoint_returns_the_ranking(client):
    async with client:
        response = await client.post("/agent/rental-requests/review", json={"listingId": LISTING})
    assert response.status_code == 200
    assert response.json()["result"]["recommendedRequestId"] == "b"


@pytest.mark.asyncio
async def test_review_endpoint_explains_missing_conflict_and_forbidden_states(client):
    async with client:
        FakeTools.rows = [req("a", "An", 3, 1)]
        assert (await client.post("/agent/rental-requests/review", json={"listingId": LISTING})).status_code == 409
        FakeTools.rows = []
        assert (await client.post("/agent/rental-requests/review", json={"listingId": LISTING})).status_code == 404
        FakeTools.error = RentalRequestToolError("no", status_code=403)
        assert (await client.post("/agent/rental-requests/review", json={"listingId": LISTING})).status_code == 403
        FakeTools.error = RentalRequestToolError("down", status_code=500)
        assert (await client.post("/agent/rental-requests/review", json={"listingId": LISTING})).status_code == 502
        assert (await client.post("/agent/rental-requests/review", json={"listingId": "short"})).status_code == 422


@pytest.mark.asyncio
async def test_ask_endpoint_sends_fresh_review_to_the_model_and_degrades_gracefully(client, monkeypatch):
    seen = {}

    class Model:
        async def generate_rental_review_answer(self, question, evidence):
            seen["question"], seen["evidence"] = question, evidence
            return "Nên chọn Bình vì thuê lâu hơn."

    monkeypatch.setattr(rental_request_review, "get_generative_client", lambda settings: Model())
    body = {"listingId": LISTING, "question": "Vì sao chọn Bình?", "history": [{"role": "user", "content": "chào"}]}
    async with client:
        ok = await client.post("/agent/rental-requests/review/ask", json=body)
        assert ok.json()["result"]["answer"].startswith("Nên chọn Bình")
        assert seen["evidence"]["review"]["recommendedRequestId"] == "b"
        assert seen["evidence"]["previousTurns"][0]["content"] == "chào"

        class Broken:
            async def generate_rental_review_answer(self, question, evidence):
                raise GenerationUnavailableError("down")

        monkeypatch.setattr(rental_request_review, "get_generative_client", lambda settings: Broken())
        degraded = (await client.post("/agent/rental-requests/review/ask", json=body)).json()["result"]
        assert degraded["status"] == "GENERATION_UNAVAILABLE" and degraded["review"]["recommendedRequestId"] == "b"
        too_long = await client.post("/agent/rental-requests/review/ask", json={**body, "question": "x" * 501})
        assert too_long.status_code == 422


@pytest.mark.asyncio
async def test_guests_cannot_use_the_review(monkeypatch):
    monkeypatch.setattr(rental_request_review, "RentalRequestTools", FakeTools)
    app.dependency_overrides[get_current_user] = lambda: UserContext(**{"X-User-Id": "guest:abc"})
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/agent/rental-requests/review", json={"listingId": LISTING})).status_code == 403
    finally:
        app.dependency_overrides.clear()
