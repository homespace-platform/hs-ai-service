from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """Success envelope shared with the existing NestJS services."""

    code: int = 1000
    message: str | None = None
    result: T | None = None
