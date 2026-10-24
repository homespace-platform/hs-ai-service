"""Landlord quick review: which of 2..n competing PENDING requests is best for the landlord."""

from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from homespace_ai.application import rental_review
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.clients.generative import GenerationUnavailableError, get_generative_client
from homespace_ai.core.api_response import ApiResponse
from homespace_ai.core.config import Settings, get_settings
from homespace_ai.core.security import UserContext, get_current_user
from homespace_ai.tools.rental_requests import RentalRequestToolError, RentalRequestTools

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/agent/rental-requests", tags=["Rental Request Review"])

MAX_TURNS = 6


class ReviewRequest(BaseModel):
    listingId: str = Field(min_length=36, max_length=36)


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=1500)


class ReviewAskRequest(ReviewRequest):
    question: str = Field(min_length=1, max_length=500)
    history: list[Turn] = Field(default_factory=list, max_length=MAX_TURNS)


def _gateway(request: Request, settings: Settings) -> tuple[GatewayApiClient, bool]:
    gateway = getattr(request.app.state, "gateway_client", None)
    if gateway is not None:
        return gateway, False
    return GatewayApiClient(str(settings.gateway_base_url), settings.gateway_request_timeout_seconds), True


async def _load_review(body: ReviewRequest, request: Request, user: UserContext,
                       settings: Settings) -> dict[str, Any]:
    if user.user_id.startswith("guest:"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Vui lòng đăng nhập để dùng tính năng này.")
    gateway, temporary = _gateway(request, settings)
    try:
        items = await RentalRequestTools(gateway, request).pending_for_listing(body.listingId)
    except RentalRequestToolError as error:
        logger.warning("rental_review_read_failed", status_code=error.status_code)
        if error.status_code in {400, 401, 403}:
            raise HTTPException(status_code=error.status_code, detail="Bạn không có quyền xem yêu cầu thuê của phòng này.") from error
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail="Chưa đọc được danh sách yêu cầu thuê lúc này. Vui lòng thử lại.") from error
    finally:
        if temporary:
            await gateway.aclose()
    if not items:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Không tìm thấy yêu cầu thuê đang chờ duyệt của phòng này.")
    if len(items) < 2:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Phòng này hiện chỉ có 1 yêu cầu đang chờ duyệt nên chưa cần so sánh.")
    first = items[0]
    return rental_review.review({"listingId": body.listingId, "listingTitle": first.get("listingTitle")}, items)


@router.post("/review", response_model=ApiResponse[dict[str, Any]])
async def review_requests(
    body: ReviewRequest, request: Request,
    user: UserContext = Depends(get_current_user), settings: Settings = Depends(get_settings),
) -> ApiResponse[dict[str, Any]]:
    """Deterministic ranking of all PENDING requests of a listing (no model call, so it is instant and free)."""
    return ApiResponse(result=await _load_review(body, request, user, settings))


@router.post("/review/ask", response_model=ApiResponse[dict[str, Any]])
async def ask_about_review(
    body: ReviewAskRequest, request: Request,
    user: UserContext = Depends(get_current_user), settings: Settings = Depends(get_settings),
) -> ApiResponse[dict[str, Any]]:
    """Free-form follow-up in the quick modal. The ranking is recomputed from fresh Core data on every question."""
    result = await _load_review(body, request, user, settings)
    evidence = {"review": result, "previousTurns": [turn.model_dump() for turn in body.history[-MAX_TURNS:]]}
    try:
        answer = await get_generative_client(settings).generate_rental_review_answer(body.question.strip(), evidence)
    except GenerationUnavailableError as error:
        logger.warning("rental_review_answer_unavailable", retryable=error.retryable)
        return ApiResponse(result={"answer": "AI chưa trả lời được lúc này. Bảng điểm bên trên vẫn là kết quả đúng, "
                                             "bạn có thể thử hỏi lại sau.",
                                   "status": "GENERATION_UNAVAILABLE", "review": result})
    return ApiResponse(result={"answer": answer, "status": "ANSWERED", "review": result})
