import pytest
from httpx import ASGITransport, AsyncClient

from homespace_ai.core.config import get_settings
from homespace_ai.main import app
from homespace_ai.repositories.conversation_repo import get_conversation_repository


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["USER", "ADMIN"])
@pytest.mark.parametrize("trust", ["missing", "wrong", "trusted"])
async def test_conversation_list_requires_gateway_trust(monkeypatch, role, trust):
    """The failing UI endpoint accepts either account role only from a trusted Gateway."""
    requested_users = []

    class ConversationStub:
        async def list_for_user(self, user_id):
            requested_users.append(user_id)
            return []

    monkeypatch.setitem(
        app.dependency_overrides, get_conversation_repository, lambda: ConversationStub()
    )
    headers = {"X-User-Id": "conversation-test-user", "X-User-Role": role}
    if trust != "missing":
        headers["X-Internal-Secret"] = (
            get_settings().gateway_internal_secret
            if trust == "trusted"
            else "incorrect-test-only-gateway-secret"
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/agent/conversations", headers=headers)

    if trust == "trusted":
        assert response.status_code == 200
        assert response.json()["result"] == []
        assert requested_users == ["conversation-test-user"]
    else:
        assert response.status_code == 403
        assert "untrusted request source" in response.json()["detail"].lower()
        assert requested_users == []


@pytest.mark.asyncio
async def test_direct_request_without_internal_secret_rejected():
    """Client sending direct request with forged X-User-Role to port 8084 is rejected (403)."""
    settings = get_settings()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Client tries to forge admin headers without the trusted internal Gateway secret
        response = await client.get(
            "/admin/knowledge/documents",
            headers={
                "X-User-Id": "attacker",
                "X-User-Role": "ADMIN",
            },
        )
        assert response.status_code == 403
        assert "untrusted request source" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_known_example_gateway_secret_is_rejected():
    settings = get_settings()
    current = settings.gateway_internal_secret
    settings.gateway_internal_secret = "hs-internal-gateway-secret-dev-2026"
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/admin/knowledge/documents",
                headers={
                    "X-User-Id": "attacker",
                    "X-User-Role": "ADMIN",
                    "X-Internal-Secret": settings.gateway_internal_secret,
                },
            )
        assert response.status_code == 503
    finally:
        settings.gateway_internal_secret = current


@pytest.mark.asyncio
async def test_regular_user_cannot_access_admin_route():
    """Authenticated regular user (ROLE: USER) is rejected with 403 on admin routes."""
    settings = get_settings()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/knowledge/documents",
            headers={
                "X-User-Id": "regular-user-123",
                "X-User-Role": "USER",
                "X-User-Authorities": "ROLE_USER",
                "X-Internal-Secret": settings.gateway_internal_secret,
            },
        )
        assert response.status_code == 403
        assert "requires admin role" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected():
    """Request without X-User-Id is rejected with 401."""
    settings = get_settings()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/agent/ask",
            json={"question": "HomeSpace là gì?"},
            headers={
                "X-Internal-Secret": settings.gateway_internal_secret,
            },
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_upload_invalid_file_rejected():
    """Upload endpoint rejects non-.md files and files exceeding size limit."""
    settings = get_settings()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Non-md file
        response = await client.post(
            "/admin/knowledge/documents",
            files={"file": ("malicious.exe", b"binarycontent", "application/octet-stream")},
            headers={
                "X-User-Id": "admin-1",
                "X-User-Role": "ADMIN",
                "X-Internal-Secret": settings.gateway_internal_secret,
            },
        )
        assert response.status_code == 400
        assert "Only .md" in response.json()["detail"]
