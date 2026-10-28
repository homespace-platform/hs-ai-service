"""Read-only listing tools. The AI service never connects to the Core database."""

from typing import Any
from math import isfinite
from uuid import UUID

from fastapi import Request

from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.core.business_properties import nearby_default_radius_m
from homespace_ai.application.listing_limits import DEFAULT_RESULTS, DEFAULT_MAX_RESULTS, ABSOLUTE_MAX_RESULTS


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
        if arguments.get("nearby") is not None:
            payload["nearby"] = arguments["nearby"]
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

    async def nearby(self, listing_ids: list[str], radius_meters: int, groups: list[str], target_place_id: str | None = None) -> dict[str, Any]:
        if not 1 <= len(listing_ids) <= min(20, self.max_results) or len(set(listing_ids)) != len(listing_ids):
            raise ListingToolError("Invalid nearby listing selection")
        for value in listing_ids:
            UUID(value)
        if not 100 <= radius_meters <= 5000:
            raise ListingToolError("Nearby radius must be 100 to 5000 meters")
        valid_groups = {"EDUCATION", "HEALTHCARE", "SHOPPING", "FOOD_DRINK", "FINANCE", "TRANSPORT", "RECREATION", "SPORTS"}
        if not groups or len(groups) > 8 or any(not isinstance(g, str) or g not in valid_groups for g in groups) or len(set(groups)) != len(groups):
            raise ListingToolError("Invalid nearby groups")
        response = await self.gateway.request("POST", f"{self.base_path}/nearby-summary", request=self.request,
            json={"listingIds": listing_ids, "radiusMeters": radius_meters, "groups": groups, "nearestPerGroup": 1,
                  "targetPlaceId": target_place_id})
        if response.status_code != 200:
            raise ListingToolError("Nearby lookup unavailable", status_code=response.status_code)
        result = response.json().get("result")
        if not isinstance(result, list) or len(result) != len(listing_ids) or {r.get("listingId") for r in result if isinstance(r, dict)} != set(listing_ids):
            raise ListingToolError("Invalid nearby evidence")
        # Bounded summaries are a contract, even if an upstream deployment drifts.
        # Reject malformed evidence rather than rank NaN distances or send a full POI dump.
        def distance(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0
        for summary in result:
            if not isinstance(summary.get("enabled"), bool) or summary.get("radiusRequestedMeters") != radius_meters:
                raise ListingToolError("Invalid nearby summary")
            if not summary["enabled"]:
                continue
            evidence_groups = summary.get("groups")
            if not isinstance(evidence_groups, list) or len(evidence_groups) != len(groups) or any(not isinstance(g, dict) for g in evidence_groups) or {g.get("code") for g in evidence_groups} != set(groups):
                raise ListingToolError("Invalid nearby group coverage")
            for group in evidence_groups:
                nearest = group.get("nearest")
                covered = group.get("radiusCoveredMeters")
                if not isinstance(group.get("coverageStatus"), str) or not isinstance(nearest, list) or len(nearest) > 1 or (covered is not None and not distance(covered)):
                    raise ListingToolError("Invalid nearby coverage")
                for place in nearest:
                    if not isinstance(place, dict) or not isinstance(place.get("name"), str) or not distance(place.get("distanceMeters")) or place["distanceMeters"] > radius_meters:
                        raise ListingToolError("Invalid nearby distance")
            if target_place_id:
                target = summary.get("target")
                if not isinstance(target, dict) or target.get("placeId") != target_place_id or not isinstance(target.get("name"), str) or not distance(target.get("distanceMeters")) or target.get("withinRadius") is not (target["distanceMeters"] <= radius_meters):
                    raise ListingToolError("Invalid nearby target evidence")
        return {"summaries": result}

    async def resolve_nearby_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        radius = int(arguments.get("radiusMeters") or nearby_default_radius_m())
        if not 100 <= radius <= 5000:
            raise ListingToolError("Nearby radius must be 100 to 5000 meters")
        nearby = {"radiusMeters": radius}
        if arguments.get("group"):
            nearby["group"] = arguments["group"]
        else:
            query = arguments.get("placeQuery")
            if not isinstance(query, str) or not 2 <= len(query.strip()) <= 160:
                raise ListingToolError("A specific place or category is required")
            response = await self.gateway.request("GET", f"{self.base_path}/nearby-places/search", request=self.request,
                params={"keyword": query.strip()})
            candidates = self._result(response).get("places", [])
            if len(candidates) != 1:
                return {"candidates": candidates[:10], "needsClarification": True}
            nearby["placeId"] = str(UUID(candidates[0]["id"]))
        return {"arguments": {"filters": arguments.get("filters", []), "sort": arguments.get("sort", "newest"),
                              "limit": arguments.get("limit", 5), "nearby": nearby}}

    @staticmethod
    def _result(response: Any) -> Any:
        if response.status_code != 200:
            raise ListingToolError(f"Core listing tool returned HTTP {response.status_code}",
                                   status_code=response.status_code)
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            raise ListingToolError("Invalid Core listing tool response")
        return data["result"]
