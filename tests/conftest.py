import pytest
import pytest_asyncio
from homespace_ai.core.config import get_settings
from homespace_ai.core.database import engine


@pytest.fixture(autouse=True)
def isolated_gateway_secret():
    settings = get_settings()
    original = settings.gateway_internal_secret
    settings.gateway_internal_secret = "test-only-gateway-secret-with-32-chars"
    yield
    settings.gateway_internal_secret = original


@pytest_asyncio.fixture(autouse=True)
async def cleanup_database_engine():
    yield
    await engine.dispose()
