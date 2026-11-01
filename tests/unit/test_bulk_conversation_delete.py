"""Bulk deletion is a physical, authenticated owner-scoped operation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from homespace_ai.core.config import get_settings
from homespace_ai.main import app
from homespace_ai.repositories.conversation_repo import ConversationRepository, get_conversation_repository


@pytest.mark.asyncio
async def test_repository_hard_deletes_all_owned_documents_in_one_operation():
    repository = ConversationRepository.__new__(ConversationRepository)
    repository.collection = SimpleNamespace(delete_many=AsyncMock(return_value=SimpleNamespace(deleted_count=17)))
    assert await repository.delete_all_for_user("account-a") == 17
    repository.collection.delete_many.assert_awaited_once_with({"ownerId": "account-a"})


@pytest.mark.asyncio
async def test_bulk_endpoint_uses_trusted_identity_is_idempotent_and_has_no_response_body():
    repository = SimpleNamespace(delete_all_for_user=AsyncMock(side_effect=[17, 0]))
    app.dependency_overrides[get_conversation_repository] = lambda: repository
    headers = {"X-User-Id": "account-a", "X-Internal-Secret": get_settings().gateway_internal_secret}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # A caller-supplied owner must never replace the authenticated identity.
            for _ in range(2):
                response = await client.delete("/agent/conversations?ownerId=account-b", headers=headers)
                assert response.status_code == 204, response.text
                assert response.content == b""
                assert response.headers["cache-control"] == "no-store"
        assert repository.delete_all_for_user.await_count == 2
        for call in repository.delete_all_for_user.await_args_list:
            assert call.args == ("account-a",)
    finally:
        app.dependency_overrides.pop(get_conversation_repository, None)


@pytest.mark.asyncio
async def test_bulk_endpoint_rejects_anonymous_and_untrusted_requests_before_deletion():
    repository = SimpleNamespace(delete_all_for_user=AsyncMock())
    app.dependency_overrides[get_conversation_repository] = lambda: repository
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            anonymous = await client.delete("/agent/conversations", headers={"X-Internal-Secret": get_settings().gateway_internal_secret})
            assert anonymous.status_code == 401
            untrusted = await client.delete("/agent/conversations", headers={"X-User-Id": "account-a"})
            assert untrusted.status_code == 403
        repository.delete_all_for_user.assert_not_awaited()
    finally:
        app.dependency_overrides.pop(get_conversation_repository, None)
