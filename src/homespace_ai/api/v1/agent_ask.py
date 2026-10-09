import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.api.v1.schemas import AskRequest, AskResponse, CitationItem
from homespace_ai.application.use_cases.ask_use_cases import AskUseCases
from homespace_ai.application.use_cases.listing_ask import ListingAsk
from homespace_ai.clients.generative import get_generative_client
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.core.api_response import ApiResponse
from homespace_ai.core.config import Settings, get_settings
from homespace_ai.core.database import get_db
from homespace_ai.core.security import UserContext, get_current_user
from homespace_ai.repositories.conversation_repo import get_conversation_repository

router = APIRouter(prefix="/agent", tags=["Agent Ask"])


@router.post(
    "/ask",
    response_model=ApiResponse[AskResponse],
)
async def ask_agent(
    body: AskRequest,
    request: Request,
    user: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    embedder=Depends(get_embedder),
) -> ApiResponse[AskResponse]:
    """Answer with role-scoped HomeSpace knowledge, or general help for ADMIN.
    """
    generative_client = get_generative_client(settings)
    use_cases = AskUseCases(
        session=session,
        settings=settings,
        embedder=embedder,
        generative_client=generative_client,
    )

    question = body.effective_question
    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Question must not be empty.",
        )

    conversation_id = body.effective_conversation_id
    conversation_repository = None
    history: list[dict[str, str]] = []
    if conversation_id:
        conversation_repository = get_conversation_repository()
        conversation = await conversation_repository.get(user.user_id, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
        history = [{"role": message.get("role", "user"), "content": message.get("content", "")}
                   for message in conversation.get("messages", [])[-8:]]
        saved = await conversation_repository.append_message(
            user.user_id, conversation_id, role="user", content=question
        )
        if not saved:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found.",
            )

    request_id = str(uuid.uuid4())
    is_admin = (user.role or "").upper() == "ADMIN" or any(
        authority.upper() in {"ADMIN", "ROLE_ADMIN"} for authority in user.authorities
    )
    result = None
    if generative_client is not None and question.strip().lower().strip(" .!?…") not in {"chào", "chào bạn", "xin chào", "hi", "hello", "hey", "chào ai", "chào homespace"}:
        gateway = getattr(request.app.state, "gateway_client", None)
        temporary_gateway = gateway is None
        if gateway is None:
            gateway = GatewayApiClient(str(settings.gateway_base_url), settings.gateway_request_timeout_seconds)
        try:
            result = await ListingAsk(settings, generative_client, gateway, request).try_answer(
                question, history, request_id)
        finally:
            if temporary_gateway:
                await gateway.aclose()
    if result is None:
        result = await use_cases.ask(
            question=question,
            user_role="ADMIN" if is_admin else "USER",
            user_name=user.name,
            locale="vi-VN",
            conversation_id=conversation_id,
            request_id=request_id,
        )

    if conversation_repository is not None:
        await conversation_repository.append_message(
            user.user_id, conversation_id, role="assistant",
            content=result.answer, status=result.status,
        )

    citations = [
        CitationItem(
            documentId=c["documentId"],
            version=c["version"],
            title=c["title"],
            heading=c["heading"],
            chunkId=c["chunkId"],
        )
        for c in result.citations
    ]

    return ApiResponse(
        code=1000,
        result=AskResponse(
            answer=result.answer,
            status=result.status,
            citations=citations,
            requestId=result.request_id,
        ),
    )
