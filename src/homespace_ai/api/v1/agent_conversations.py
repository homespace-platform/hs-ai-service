"""User-scoped persistent AI conversation history."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from homespace_ai.api.v1.schemas import (
    ConversationDetail,
    ConversationMessage,
    ConversationSummary,
    SetConversationPinnedRequest,
    GuestStateRequest,
)
from homespace_ai.core.api_response import ApiResponse
from homespace_ai.core.security import UserContext, get_current_user
from homespace_ai.core.config import get_settings
from homespace_ai.core.guest_state import decode_state
from homespace_ai.repositories.conversation_repo import (
    ConversationRepository,
    get_conversation_repository,
)

router = APIRouter(prefix="/agent/conversations", tags=["Agent Conversations"])


def summary(document: dict) -> ConversationSummary:
    return ConversationSummary(
        id=document["_id"],
        title=document["title"],
        isPinned=document["isPinned"],
        createdAt=document["createdAt"],
        updatedAt=document["updatedAt"],
    )


def detail(document: dict) -> ConversationDetail:
    return ConversationDetail(
        **summary(document).model_dump(),
        messages=[ConversationMessage.model_validate(message) for message in document["messages"]],
    )


@router.get("", response_model=ApiResponse[list[ConversationSummary]])
async def list_conversations(
    user: UserContext = Depends(get_current_user),
    repository: ConversationRepository = Depends(get_conversation_repository),
) -> ApiResponse[list[ConversationSummary]]:
    documents = await repository.list_for_user(user.user_id)
    return ApiResponse(result=[summary(document) for document in documents])


@router.post("", status_code=201, response_model=ApiResponse[ConversationDetail])
async def create_conversation(
    user: UserContext = Depends(get_current_user),
    repository: ConversationRepository = Depends(get_conversation_repository),
) -> ApiResponse[ConversationDetail]:
    document = await repository.create(user.user_id)
    return ApiResponse(result=detail(document))


@router.post("/claim-guest", response_model=ApiResponse[ConversationDetail])
async def claim_guest(body: GuestStateRequest, request: Request, response: Response,
                      user: UserContext = Depends(get_current_user),
                      repository=Depends(get_conversation_repository)):
    from homespace_ai.api.v1.guest_agent import guest_owner
    state = decode_state(body.guestState, guest_owner(request), get_settings())
    if not state["messages"] or user.user_id.startswith("guest:"):
        raise HTTPException(400, "No guest conversation to transfer.")
    document = await repository.claim_guest(state, user.user_id)
    if document is None:
        raise HTTPException(409, "Guest conversation is stale or already belongs to another account.")
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(result=detail(document))


@router.get("/{conversation_id}", response_model=ApiResponse[ConversationDetail])
async def get_conversation(
    conversation_id: UUID,
    user: UserContext = Depends(get_current_user),
    repository: ConversationRepository = Depends(get_conversation_repository),
) -> ApiResponse[ConversationDetail]:
    document = await repository.get(user.user_id, str(conversation_id))
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return ApiResponse(result=detail(document))


@router.patch("/{conversation_id}/pin", response_model=ApiResponse[ConversationSummary])
async def pin_conversation(
    conversation_id: UUID,
    body: SetConversationPinnedRequest,
    user: UserContext = Depends(get_current_user),
    repository: ConversationRepository = Depends(get_conversation_repository),
) -> ApiResponse[ConversationSummary]:
    updated = await repository.set_pinned(user.user_id, str(conversation_id), body.isPinned)
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    document = await repository.get(user.user_id, str(conversation_id))
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return ApiResponse(result=summary(document))


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: UUID,
    user: UserContext = Depends(get_current_user),
    repository: ConversationRepository = Depends(get_conversation_repository),
) -> None:
    deleted = await repository.delete(user.user_id, str(conversation_id))
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
