from fastapi import APIRouter, Depends

from homespace_ai.core.api_response import ApiResponse
from homespace_ai.core.security import UserContext, get_current_user

router = APIRouter()


@router.get("/ping", tags=["Health"])
async def ping() -> str:
    return "pong"


@router.get(
    "/auth/me",
    tags=["Authentication"],
    response_model=ApiResponse[dict[str, object]],
    response_model_exclude_none=True,
)
async def current_identity(
    user: UserContext = Depends(get_current_user),
) -> ApiResponse[dict[str, object]]:
    """Diagnostic endpoint matching the identity contract used by NestJS services."""
    result: dict[str, object] = {
        "userId": user.user_id,
        "authorities": user.authorities,
    }
    if user.email:
        result["email"] = user.email
    if user.name:
        result["name"] = user.name
    if user.role:
        result["role"] = user.role
    return ApiResponse(result=result)
