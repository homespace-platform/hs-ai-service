import httpx
from fastapi import HTTPException, Request, status


class GatewayApiClient:
    """Call HomeSpace APIs through Gateway, forwarding the caller's JWT."""

    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        request: Request,
        params: dict[str, str] | None = None,
        json: object | None = None,
    ) -> httpx.Response:
        if not path.startswith("/api/v1/") or path.startswith("/api/v1/ai/"):
            raise ValueError("Gateway client only accepts non-AI /api/v1 paths")

        authorization = request.headers.get("authorization")
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token for downstream Gateway request",
            )

        try:
            return await self._client.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=json,
                headers={"Authorization": authorization},
            )
        except httpx.TimeoutException as error:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Timed out while calling HomeSpace API Gateway",
            ) from error
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not reach HomeSpace API Gateway",
            ) from error
