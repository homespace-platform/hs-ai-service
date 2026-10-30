import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.api.v1.schemas import AppointmentCardItem, AskRequest, AskResponse, CitationItem, ContractCardItem
from homespace_ai.application.use_cases.ask_use_cases import AskUseCases, AskResult
from homespace_ai.application import appointment_context
from homespace_ai.application.use_cases.appointment_ask import AppointmentAsk
from homespace_ai.application.contract_context import classify
from homespace_ai.application.use_cases.contract_ask import ContractAsk
from homespace_ai.application.use_cases.listing_ask import ListingAsk
from homespace_ai.clients.generative import get_generative_client
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.core.api_response import ApiResponse
from homespace_ai.core.config import Settings, get_settings
from homespace_ai.core.database import get_db
from homespace_ai.core.security import UserContext, get_current_user
from homespace_ai.repositories.conversation_repo import conversation_search_state, get_conversation_repository

router = APIRouter(prefix="/agent", tags=["Agent Ask"])
GREETING_TEXTS = {"chào", "chào bạn", "xin chào", "hi", "hello", "hey", "chào ai", "chào homespace"}


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
    question = body.effective_question
    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Question must not be empty.",
        )

    conversation_id = body.effective_conversation_id
    conversation_repository = None
    history: list[dict[str, Any]] = []
    search_state = None
    if conversation_id:
        conversation_repository = get_conversation_repository()
        conversation = await conversation_repository.get(user.user_id, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
        # Criteria survive beyond the eight-message prompt window; they belong to this owner's conversation only.
        search_state = conversation_search_state(conversation)
        history = [{"role": message.get("role", "user"), "content": message.get("content", ""),
                    "status": message.get("status"), "listingContext": message.get("listingContext"),
                    "contractContext": message.get("contractContext"),
                    "appointmentContext": message.get("appointmentContext")}
                   for message in conversation.get("messages", [])[-8:]]
        saved = await conversation_repository.append_message(
            user.user_id, conversation_id, role="user", content=question
        )
        if not saved:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found.",
            )

    result = await generate_answer(question, history, request, user, session, settings, embedder, conversation_id,
                                   search_state=search_state)

    if conversation_repository is not None:
        await conversation_repository.append_message(
            user.user_id, conversation_id, role="assistant",
            content=stored_answer(result), status=result.status,
            listing_context=result.listing_context,
            listing_ids=result.listing_ids,
            listing_positions=result.listing_positions,
            contract_context=result.contract_context, contract_cards=result.contract_cards,
            appointment_context=result.appointment_context, appointment_cards=result.appointment_cards,
            data_sources=result.data_sources,
            suggested_questions=result.suggested_questions,
        )
    return public_answer(result)


def stored_answer(result: AskResult) -> str:
    # Keep references/preferences, not a permanent second copy of expiring provider facts in Mongo/JWT.
    if "CORE_NEARBY" in result.data_sources:
        return "Đã đối chiếu tiện ích xung quanh của nhóm phòng. Hỏi lại để đọc dữ liệu mới nhất; thông tin địa điểm không được lưu cố định trong lịch sử."
    return result.answer


async def generate_answer(
    question: str, history: list[dict[str, Any]], request: Request, user: UserContext,
    session: AsyncSession, settings: Settings, embedder, conversation_id: str | None = None,
    search_state: dict[str, Any] | None = None,
) -> AskResult:
    """Shared pipeline; persistence is explicitly handled by each caller."""
    generative_client = get_generative_client(settings)
    use_cases = AskUseCases(session=session, settings=settings, embedder=embedder,
                            generative_client=generative_client)
    request_id = str(uuid.uuid4())
    is_admin = (user.role or "").upper() == "ADMIN" or any(
        authority.upper() in {"ADMIN", "ROLE_ADMIN"} for authority in user.authorities
    )
    result = None
    is_guest = user.user_id.startswith("guest:")
    if question.strip().lower().strip(" .!?…") not in GREETING_TEXTS and appointment_context.classify(question, history) is not None:
        # Viewing appointments come first: the old personal-data regex in RAG must never answer these, and the
        # Contract module must not capture ordinary appointment words.
        gateway = getattr(request.app.state, "gateway_client", None)
        temporary_gateway = gateway is None
        if gateway is None:
            gateway = GatewayApiClient(str(settings.gateway_base_url), settings.gateway_request_timeout_seconds)

        async def appointment_guidance(general_question: str) -> AskResult:
            return await use_cases.ask(question=general_question, user_role="USER", user_name=None,
                                       locale="vi-VN", conversation_id=conversation_id, request_id=request_id)

        try:
            result = await AppointmentAsk(settings, generative_client, gateway, request, is_guest=is_guest,
                                          rag=appointment_guidance).try_answer(question, history, request_id)
        finally:
            if temporary_gateway:
                await gateway.aclose()
        if result is not None:
            return result
    if question.strip().lower().strip(" .!?…") not in GREETING_TEXTS and classify(question, history) is not None:
        # The account's own contracts come first: the personal-data regex in RAG must never answer these.
        gateway = getattr(request.app.state, "gateway_client", None)
        temporary_gateway = gateway is None
        if gateway is None:
            gateway = GatewayApiClient(str(settings.gateway_base_url), settings.gateway_request_timeout_seconds)

        async def general_guidance(general_question: str) -> AskResult:
            return await use_cases.ask(question=general_question, user_role="USER", user_name=None,
                                       locale="vi-VN", conversation_id=conversation_id, request_id=request_id)

        try:
            result = await ContractAsk(settings, generative_client, gateway, request, is_guest=is_guest,
                                       rag=general_guidance).try_answer(question, history, request_id)
        finally:
            if temporary_gateway:
                await gateway.aclose()
        if result is not None:
            return result
    if generative_client is not None and question.strip().lower().strip(" .!?…") not in {"chào", "chào bạn", "xin chào", "hi", "hello", "hey", "chào ai", "chào homespace"}:
        gateway = getattr(request.app.state, "gateway_client", None)
        temporary_gateway = gateway is None
        if gateway is None:
            gateway = GatewayApiClient(str(settings.gateway_base_url), settings.gateway_request_timeout_seconds)
        try:
            result = await ListingAsk(settings, generative_client, gateway, request).try_answer(
                question, history, request_id, state=search_state)
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

    return result


def public_answer(result: AskResult) -> ApiResponse[AskResponse]:

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
            listingIds=result.listing_ids,
            listingPositions=result.listing_positions,
            contractCards=[ContractCardItem(**card) for card in result.contract_cards],
            appointmentCards=[AppointmentCardItem(**card) for card in result.appointment_cards],
            dataSources=result.data_sources,
            suggestedQuestions=result.suggested_questions,
        ),
    )
