from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homespace_ai.application.listing_context import context_for
from homespace_ai.repositories.conversation_repo import ConversationRepository
from homespace_ai.api.v1.schemas import ConversationMessage


@pytest.mark.asyncio
async def test_repository_persists_only_assistant_reference_state_and_preserves_owner_filter():
    repo = ConversationRepository.__new__(ConversationRepository)
    repo.collection = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)))
    state = context_for(["00000000-0000-0000-0000-000000000001"])
    assert await repo.append_message("owner", "conv", role="assistant", content="Phòng thử nghiệm",
                                     status="ANSWERED", listing_context=state)
    call = repo.collection.update_one.call_args
    assert call.args[0] == {"_id": "conv", "ownerId": "owner"}
    message = call.args[1]["$push"]["messages"]
    assert message["listingContext"] == state
    assert "listingContext" not in ConversationMessage.model_validate(message).model_dump()
    await repo.append_message("owner", "conv", role="user", content="Chọn giúp tôi", listing_context=state)
    first_call = repo.collection.update_one.call_args_list[1]
    assert "listingContext" not in first_call.args[1]["$push"]["messages"]
