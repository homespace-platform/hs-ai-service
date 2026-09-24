import base64
import binascii
import hmac

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from homespace_ai.core.config import get_settings


class UserContext(BaseModel):
    user_id: str = Field(alias="X-User-Id")
    email: str | None = Field(default=None, alias="X-User-Email")
    name: str | None = Field(default=None, alias="X-User-Name")
    name_b64: str | None = Field(default=None, alias="X-User-Name-B64")
    role: str | None = Field(default=None, alias="X-User-Role")
    authorities: list[str] = Field(default_factory=list, alias="X-User-Authorities")


async def verify_gateway_secret(request: Request) -> None:
    """Verifies that the request carries the trusted internal Gateway token.
    Prevents clients from connecting directly to port 8084 with spoofed X-User-* headers.
    """
    settings = get_settings()
    expected_secret = settings.gateway_internal_secret.strip()
    if expected_secret:
        if expected_secret == "hs-internal-gateway-secret-dev-2026" or len(expected_secret) < 32:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Gateway trust is not securely configured.",
            )

        incoming_secret = request.headers.get("x-internal-secret", "").strip()
        if not incoming_secret or not hmac.compare_digest(incoming_secret, expected_secret):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: untrusted request source.",
            )
        return

    # In production/staging, secret is strictly required
    if settings.app_env != "local":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway trust is not securely configured.",
        )


async def get_current_user(
    request: Request,
    _: None = Depends(verify_gateway_secret),
) -> UserContext:
    """Read the identity headers added by the trusted API Gateway."""
    user_id = request.headers.get("x-user-id", "").strip()
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthenticated",
        )

    raw_authorities = request.headers.get("x-user-authorities", "")
    encoded_name = request.headers.get("x-user-name-b64")
    decoded_name = None
    if encoded_name:
        try:
            decoded_name = base64.b64decode(encoded_name, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            decoded_name = None

    return UserContext(
        **{
            "X-User-Id": user_id,
            "X-User-Email": request.headers.get("x-user-email"),
            "X-User-Name": decoded_name or request.headers.get("x-user-name"),
            "X-User-Name-B64": request.headers.get("x-user-name-b64"),
            "X-User-Role": request.headers.get("x-user-role"),
            "X-User-Authorities": [
                authority.strip()
                for authority in raw_authorities.split(",")
                if authority.strip()
            ],
        }
    )


async def require_admin(user: UserContext = Depends(get_current_user)) -> UserContext:
    """Ensures that the current user possesses the ADMIN role."""
    role = (user.role or "").upper().strip()
    authorities = [a.upper().strip() for a in user.authorities]

    if role != "ADMIN" and "ADMIN" not in authorities and "ROLE_ADMIN" not in authorities:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: requires ADMIN role.",
        )
    return user
