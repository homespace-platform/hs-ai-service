import base64
import binascii

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, Field


class UserContext(BaseModel):
    user_id: str = Field(alias="X-User-Id")
    email: str | None = Field(default=None, alias="X-User-Email")
    name: str | None = Field(default=None, alias="X-User-Name")
    name_b64: str | None = Field(default=None, alias="X-User-Name-B64")
    role: str | None = Field(default=None, alias="X-User-Role")
    authorities: list[str] = Field(default_factory=list, alias="X-User-Authorities")


async def get_current_user(request: Request) -> UserContext:
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
