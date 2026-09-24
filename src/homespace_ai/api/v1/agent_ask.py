import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.api.v1.schemas import AskRequest, AskResponse, CitationItem
from homespace_ai.application.use_cases.ask_use_cases import AskUseCases
from homespace_ai.clients.generative import get_generative_client
from homespace_ai.core.api_response import ApiResponse
from homespace_ai.core.config import Settings, get_settings
from homespace_ai.core.database import get_db
from homespace_ai.core.security import UserContext, get_current_user

router = APIRouter(prefix="/agent", tags=["Agent Ask"])


@router.post(
    "/ask",
    response_model=ApiResponse[AskResponse],
)
async def ask_agent(
    body: AskRequest,
    user: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    embedder=Depends(get_embedder),
) -> ApiResponse[AskResponse]:
    """User-facing RAG ask endpoint.
    Retrieves facts from approved active knowledge chunks and synthesizes an answer with citations.
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
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Question must not be empty.",
        )

    request_id = str(uuid.uuid4())
    result = await use_cases.ask(
        question=question,
        user_role=user.role,
        locale="vi-VN",
        conversation_id=body.effective_conversation_id,
        request_id=request_id,
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
