"""Anonymous AI with browser-memory, signed context and metadata-only quotas."""
import hashlib
import re
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from homespace_ai.api.v1.admin_knowledge import get_embedder
from homespace_ai.api.v1.agent_ask import generate_answer, public_answer, stored_answer
from homespace_ai.api.v1.schemas import GuestAskRequest, GuestAskResponse, GuestConversationDetail, ConversationSummary
from homespace_ai.core.api_response import ApiResponse
from homespace_ai.core.config import Settings, get_settings
from homespace_ai.core.database import get_db
from homespace_ai.core.guest_state import encode_state, decode_state
from homespace_ai.core.security import UserContext, verify_gateway_secret
from homespace_ai.repositories.conversation_repo import get_conversation_repository

router = APIRouter(prefix="/guest/agent", tags=["Guest AI"])
COOKIE_NAME = "hs_ai_guest"


def guest_owner(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(403, "Guest session cookie required.")
    return "guest:" + hashlib.sha256(token.encode()).hexdigest()


async def get_guest_user(request: Request, response: Response,
                         _: None = Depends(verify_gateway_secret),
                         settings: Settings = Depends(get_settings), repository=Depends(get_conversation_repository)):
    token = request.cookies.get(COOKIE_NAME, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        token = secrets.token_urlsafe(32)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", path="/",
                            secure=settings.app_env != "local")
    response.headers["Cache-Control"] = "no-store"
    if request.method == "POST":
        peer = request.headers.get("x-ai-client-ip") if settings.gateway_internal_secret else None
        peer = peer or (request.client.host if request.client else "unknown")
        retry_after = await repository.consume_guest_quota(hashlib.sha256(peer.encode()).hexdigest())
        if retry_after:
            raise HTTPException(429, "Bạn đã hỏi khá nhiều. Vui lòng thử lại sau.", headers={"Retry-After": str(retry_after)})
    return UserContext(**{"X-User-Id": "guest:" + hashlib.sha256(token.encode()).hexdigest(),
                          "X-User-Role": "USER", "X-User-Authorities": ["ROLE_USER"]})


@router.get("/conversations", response_model=ApiResponse[list[ConversationSummary]])
async def list_conversations(user=Depends(get_guest_user)):
    return ApiResponse(result=[])  # Never expose saved guest history, including legacy records.


@router.post("/conversations", status_code=201, response_model=ApiResponse[GuestConversationDetail])
async def create_conversation(user=Depends(get_guest_user), settings=Depends(get_settings), repository=Depends(get_conversation_repository)):
    now = datetime.now(timezone.utc)
    identifier = str(uuid4())
    state = {"id": identifier, "owner": user.user_id, "messages": [], "count": 0}
    encode_state(state, settings)  # Fail closed before metadata if no signing key.
    budget = await repository.start_guest(user.user_id, identifier)
    state["count"] = budget["count"]
    return ApiResponse(result=GuestConversationDetail(
        id=identifier, title="Đoạn chat mới", isPinned=False, createdAt=now, updatedAt=now, messages=[],
        guestState=encode_state(state, settings), remainingQuestions=max(0, settings.guest_chat_max_questions - state["count"])))


@router.post("/ask", response_model=ApiResponse[GuestAskResponse])
async def ask(body: GuestAskRequest, request: Request, user=Depends(get_guest_user),
              session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
              embedder=Depends(get_embedder), repository=Depends(get_conversation_repository)):
    question = body.effective_question
    if not question:
        raise HTTPException(400, "Question must not be empty.")
    state = decode_state(body.guestState, user.user_id, settings)
    if state["id"] != body.effective_conversation_id:
        raise HTTPException(403, "Guest conversation does not match context.")
    if state["count"] >= settings.guest_chat_max_questions:
        raise HTTPException(403, detail={"code": "GUEST_LOGIN_REQUIRED", "message": "Đăng nhập để tiếp tục hỏi AI."})
    reserved = await repository.reserve_guest_question(user.user_id, state["id"], state["count"])
    if not reserved:
        raise HTTPException(409, detail={"code": "GUEST_CONTEXT_STALE", "message": "Phiên khách đã thay đổi. Vui lòng đăng nhập để tiếp tục."})
    old_count = state["count"]
    try:
        result = await generate_answer(question, state["messages"][-8:], request, user, session, settings, embedder, state["id"])
        now = datetime.now(timezone.utc).isoformat()
        state["messages"].extend([
            {"id": str(uuid4()), "role": "user", "content": question, "createdAt": now},
            {"id": str(uuid4()), "role": "assistant", "content": stored_answer(result), "createdAt": now,
             "status": result.status, "listingContext": result.listing_context,
             "listingIds": result.listing_ids, "listingPositions": result.listing_positions,
             "contractContext": result.contract_context, "contractCards": result.contract_cards,
             "appointmentContext": result.appointment_context, "appointmentCards": result.appointment_cards,
             "dataSources": result.data_sources},
        ])
        state["count"] = reserved["count"]
        return ApiResponse(result=GuestAskResponse(**public_answer(result).result.model_dump(),
            guestState=encode_state(state, settings), remainingQuestions=max(0, settings.guest_chat_max_questions - state["count"])))
    except Exception:
        await repository.release_guest_question(user.user_id, state["id"], old_count)
        raise
