import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.api.v1.schemas import AskRequest, AskResponse, CitationItem, PropertySearchContext
from homespace_ai.application.use_cases.ask_use_cases import AskResult, AskUseCases
from homespace_ai.clients.generative import get_generative_client
from homespace_ai.core.api_response import ApiResponse
from homespace_ai.core.config import Settings, get_settings
from homespace_ai.core.database import get_db
from homespace_ai.core.security import UserContext, get_current_user
from homespace_ai.repositories.conversation_repo import get_conversation_repository
from homespace_ai.property_search.intent import parse_intent
from homespace_ai.property_search.field_qa import answer_listing_question, is_detail_followup
from homespace_ai.property_search.mcp_client import call_listing_tool
from homespace_ai.property_search.response import (
    asks_about_parking,
    compose_search_reply,
    previous_result_ids,
    references_previous_results,
)

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
    search_context = body.searchContext
    conversation_history: list[dict] = []
    search_state: dict | None = None
    if conversation_id:
        conversation_repository = get_conversation_repository()
        saved_conversation = await conversation_repository.get(user.user_id, conversation_id)
        if not saved_conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found.",
            )
        conversation_history = saved_conversation.get("messages", [])
        search_state = saved_conversation.get("searchState")
        if search_context:
            await conversation_repository.set_search_context(
                user.user_id,
                conversation_id,
                search_context.model_dump(exclude_none=True),
            )
        else:
            saved_search_context = saved_conversation.get("searchContext") if saved_conversation else None
            if saved_search_context:
                search_context = PropertySearchContext.model_validate(saved_search_context)
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
    if search_context:
        previous_user_messages = [
            str(message.get("content", ""))
            for message in conversation_history
            if message.get("role") == "user" and message.get("content")
        ][-8:]
        context = search_context
        resolved_location = await call_listing_tool("resolve_listing_ward", {
            "province_code": context.provinceCode,
            "query": question,
        })
        explicit_district = resolved_location.get("ward")
        district = explicit_district or context.district
        prior_ids = previous_result_ids(search_state, conversation_history)
        changed_ward = bool(explicit_district and explicit_district != context.district)
        if is_detail_followup(question, bool(prior_ids), changed_ward):
            facts = await call_listing_tool("get_listing_facts", {"listing_ids": prior_ids})
            answer = await answer_listing_question(
                question=question,
                facts=facts,
                previous_questions=previous_user_messages,
                generative_client=generative_client,
            )
        else:
            intent = await parse_intent(question, previous_user_messages=previous_user_messages)
            scope_to_previous = references_previous_results(
                question, bool(prior_ids), changed_ward
            )
            if explicit_district:
                search_context = PropertySearchContext(
                    provinceCode=context.provinceCode,
                    district=explicit_district,
                    category=context.category,
                )
                if conversation_repository is not None and conversation_id:
                    await conversation_repository.set_search_context(
                        user.user_id,
                        conversation_id,
                        search_context.model_dump(exclude_none=True),
                    )
            category = intent.category if intent.category in {"ROOM", "APARTMENT", "HOUSE"} else context.category
            result_data = await call_listing_tool("search_listings", {
                "province_code": context.provinceCode,
                "district": district or "",
                "category": category or "",
                "price_max": intent.price_max or 0,
                "has_mezzanine": intent.has_mezzanine,
                "has_balcony": intent.has_balcony,
                "has_parking": intent.has_parking and not (
                    scope_to_previous and asks_about_parking(question)
                ),
                "has_garage": intent.has_garage,
                "listing_ids": prior_ids if scope_to_previous else [],
                "has_video": False,
                "location": "" if district else (intent.location or ""),
                "landmark": intent.landmark or "",
                "page": 1,
                "size": 5,
                "sort": "newest",
            })
            answer = await compose_search_reply(
                question=question,
                result_data=result_data,
                previous_ids=prior_ids,
                scoped=scope_to_previous,
            )
            if conversation_repository is not None and conversation_id:
                result_ids = [item["id"] for item in result_data.get("matches", [])]
                await conversation_repository.set_search_state(
                    user.user_id,
                    conversation_id,
                    {"resultIds": result_ids,
                     "referenceIds": result_ids or (prior_ids if scope_to_previous else [])},
                )
        result = AskResult(
            answer=answer,
            status="PROPERTY_SEARCH",
            citations=[],
            request_id=request_id,
        )
    else:
        result = await use_cases.ask(
            question=question,
            user_role="ADMIN" if is_admin else "USER",
            user_name=user.name,
            locale="vi-VN",
            conversation_id=conversation_id,
            request_id=request_id,
            conversation_history=conversation_history,
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
