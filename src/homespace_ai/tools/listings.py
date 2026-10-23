"""Read-only listing tools. The AI service never connects to the Core database."""

from typing import Any
from uuid import UUID

from fastapi import Request

from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.application.listing_limits import DEFAULT_RESULTS, DEFAULT_MAX_RESULTS, ABSOLUTE_MAX_RESULTS


MAX_SURROUNDING_CANDIDATES = 10
SURROUNDINGS_TIMEOUT_SECONDS = 40.0  # Core's own scan deadline is 25 s; the default 15 s client timeout is too short


class ListingToolError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ListingTools:
    def __init__(self, gateway: GatewayApiClient, request: Request, max_results: int = DEFAULT_MAX_RESULTS) -> None:
        self.gateway = gateway
        self.request = request
        self.base_path = "/api/v1/public/listings/ai"
        self.max_results = min(max(max_results, 1), ABSOLUTE_MAX_RESULTS)

    async def fields(self) -> dict[str, str]:
        response = await self.gateway.request("GET", f"{self.base_path}/fields", request=self.request)
        return self._result(response)

    async def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        filters = arguments.get("filters")
        if not isinstance(filters, list) or len(filters) > 24:
            raise ListingToolError("Invalid listing filters")
        payload = {"filters": filters, "sort": arguments.get("sort", "newest"),
                   "limit": min(max(int(arguments.get("limit") or DEFAULT_RESULTS), 1), self.max_results)}
        keyword = arguments.get("keyword")
        if isinstance(keyword, str) and keyword.strip():
            payload["keyword"] = keyword.strip()[:160]
        response = await self.gateway.request("POST", f"{self.base_path}/search",
                                              request=self.request, json=payload)
        result = self._result(response)
        ids = result.get('listingIds')
        if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
            raise ListingToolError('Invalid Core search result IDs')
        # Defense against a changed/misconfigured upstream: never fan out into 1,000 detail requests.
        return {name: result[name] for name in ('total', 'readAt') if name in result} | {
            'listingIds': list(dict.fromkeys(ids))[:payload['limit']]}

    async def compare_surroundings(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Authenticated nearby-amenity comparison. Core owns coordinates, Geoapify access and ranking."""
        ids = arguments.get("listingIds")
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SURROUNDING_CANDIDATES:
            raise ListingToolError("Invalid surroundings listing IDs")
        try:
            safe_ids = list(dict.fromkeys(str(UUID(str(value))) for value in ids))
        except ValueError as error:
            raise ListingToolError("Invalid listing ID") from error
        payload: dict[str, Any] = {
            "listingIds": safe_ids,
            "renterProfiles": list(arguments.get("renterProfiles") or []),
            "priorityCategories": list(arguments.get("priorityCategories") or []),
        }
        if arguments.get("radiusMeters") is not None:
            payload["radiusMeters"] = int(arguments["radiusMeters"])
        if arguments.get("targetPlaceQuery"):
            payload["targetPlaceQuery"] = str(arguments["targetPlaceQuery"])[:120]
        if isinstance(arguments.get("originalPositions"), dict):
            payload["originalPositions"] = {str(key): int(value) for key, value in arguments["originalPositions"].items()}
        response = await self.gateway.request("POST", "/api/v1/listings/ai/surroundings/compare",
                                              request=self.request, json=payload,
                                              timeout=SURROUNDINGS_TIMEOUT_SECONDS)
        result = self._result(response)
        recommended = result.get("recommendedIds")
        if not isinstance(recommended, list) or not all(isinstance(value, str) for value in recommended):
            raise ListingToolError("Invalid Core surroundings result")
        return result

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
                   "publishedAt", "expiresAt", "viewCount", "policies")
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
            raise ListingToolError(f"Core listing tool returned HTTP {response.status_code}",
                                   status_code=response.status_code)
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            raise ListingToolError("Invalid Core listing tool response")
        return data["result"]
