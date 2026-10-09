"""Read-only listing tools. The AI service never connects to the Core database."""

from typing import Any
from uuid import UUID

from fastapi import Request

from homespace_ai.clients.gateway import GatewayApiClient


class ListingToolError(Exception):
    pass


class ListingTools:
    def __init__(self, gateway: GatewayApiClient, request: Request) -> None:
        self.gateway = gateway
        self.request = request
        self.base_path = "/api/v1/public/listings/ai"

    async def fields(self) -> dict[str, str]:
        response = await self.gateway.request("GET", f"{self.base_path}/fields", request=self.request)
        return self._result(response)

    async def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        filters = arguments.get("filters")
        if not isinstance(filters, list) or len(filters) > 24:
            raise ListingToolError("Invalid listing filters")
        payload = {"filters": filters, "sort": arguments.get("sort", "newest"),
                   "limit": min(max(int(arguments.get("limit") or 5), 1), 5)}
        response = await self.gateway.request("POST", f"{self.base_path}/search",
                                              request=self.request, json=payload)
        return self._result(response)

    async def detail(self, listing_id: str) -> dict[str, Any]:
        try:
            safe_id = str(UUID(listing_id))
        except ValueError as error:
            raise ListingToolError("Invalid listing ID") from error
        response = await self.gateway.request("GET", f"{self.base_path}/{safe_id}", request=self.request)
        result = self._result(response)
        if result.get("status") != "PUBLISHED" or result.get("active") is not True:
            raise ListingToolError("Listing is no longer public")
        # Never send owner identity, audit fields or internal moderation data to the model.
        allowed = ("id", "title", "description", "category", "status", "availableFrom",
                   "areaM2", "maxMotorbikeCount", "maxCarCount", "pricing", "houseDetail",
                   "apartmentDetail", "roomDetail", "amenities", "customAmenities",
                   "furnishings", "charges", "address", "viewingDays", "viewingSlots",
                   "publishedAt", "expiresAt", "viewCount")
        safe = {key: result.get(key) for key in allowed if key in result}
        media = result.get("media") or []
        safe["mediaCounts"] = {
            "images": sum(1 for item in media if item.get("mediaType") == "IMAGE"),
            "videos": sum(1 for item in media if item.get("mediaType") == "VIDEO"),
        }
        return safe

    @staticmethod
    def _result(response: Any) -> Any:
        if response.status_code != 200:
            raise ListingToolError(f"Core listing tool returned HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            raise ListingToolError("Invalid Core listing tool response")
        return data["result"]
