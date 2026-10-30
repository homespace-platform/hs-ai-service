"""Read-only listing tools. The AI service never connects to the Core database."""

import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any
from math import isfinite
from uuid import UUID

from fastapi import Request

from homespace_ai.application.vietnamese_text import fold_text
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.core.business_properties import nearby_default_radius_m
from homespace_ai.application.listing_limits import DEFAULT_RESULTS, DEFAULT_MAX_RESULTS, ABSOLUTE_MAX_RESULTS


class ListingToolError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ListingSearchRejected(ListingToolError):
    """Core refused a criterion (field/enum/operator/target). A diagnosis, not an outage or zero results."""

    def __init__(self, reason: str, field: str | None, allowed: list[str]) -> None:
        super().__init__(f"Core rejected the search: {reason}", status_code=400)
        self.reason = reason
        self.field = field
        self.allowed = allowed


V2_RESPONSE_CHECKS = {"MATCH", "NOT_MATCH", "UNKNOWN"}
SAFE_COVER = re.compile(r"^https://[^\s<>()|\\\"']+$")


@lru_cache(maxsize=1)
def place_aliases() -> dict[str, str]:
    data = json.loads(files("homespace_ai").joinpath("data/nearby_place_aliases.json").read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("aliases"), dict):
        raise ValueError("Invalid nearby place alias data")
    return {fold_text(key): str(value) for key, value in data["aliases"].items()}


def place_query(text: str) -> str:
    """Managed abbreviations ("IUH") become searchable names; Core matches accents-insensitively."""
    folded = re.sub(r"\s+", " ", fold_text(text)).strip()
    folded = re.sub(r"^(?:truong|co so)\s+(?=dh\b|dai hoc\b)", "", folded)
    for alias, name in sorted(place_aliases().items(), key=lambda item: -len(item[0])):
        if re.search(r"\b" + re.escape(alias) + r"\b", folded):
            return name
    return re.sub(r"^(?:trường|cơ sở)\s+(?=đh\b|đại học\b)", "", text.strip(), flags=re.IGNORECASE)[:160]


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

    async def catalog(self) -> dict[str, Any]:
        response = await self.gateway.request("GET", f"{self.base_path}/catalog", request=self.request)
        return self._result(response)

    async def search_v2(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Typed v2 search: every argument the planner produced reaches Core, or the call fails loudly."""
        allowed = {"version", "filters", "where", "sort", "limit", "keyword", "nearbyAll", "context",
                   "scopeIds", "excludeIds", "evidence"}
        if payload.get("version") != 2 or set(payload) - allowed:
            raise ListingToolError("Unsupported listing search contract")
        limit = payload.get("limit")
        if type(limit) is not int or not 1 <= limit <= self.max_results:
            raise ListingToolError("Invalid listing search limit")
        for name in ("scopeIds", "excludeIds"):
            if name in payload:
                payload[name] = [str(UUID(str(value))) for value in payload[name]][:ABSOLUTE_MAX_RESULTS]
        response = await self.gateway.request("POST", f"{self.base_path}/search", request=self.request, json=payload)
        if response.status_code == 400:
            body = self._json(response)
            detail = body.get("result") if isinstance(body.get("result"), dict) else {}
            reason = detail.get("reason") if isinstance(detail.get("reason"), str) else "INVALID_REQUEST"
            raise ListingSearchRejected(reason, detail.get("field") if isinstance(detail.get("field"), str) else None,
                                        [str(v) for v in detail.get("allowed") or []][:40])
        result = self._result(response)
        ids = result.get("listingIds")
        if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids) or result.get("version") != 2:
            raise ListingToolError("Invalid Core v2 search result")
        ids = list(dict.fromkeys(ids))[:limit]
        if "scopeIds" in payload and any(value not in payload["scopeIds"] for value in ids):
            raise ListingToolError("Core returned a listing outside the requested scope")
        evidence = []
        for item in result.get("evidence") or []:
            if not isinstance(item, dict) or item.get("listingId") not in ids:
                continue
            checks = [{"fields": [str(f) for f in (c.get("fields") or [])][:6], "status": c.get("status"),
                       "actual": c.get("actual") if isinstance(c.get("actual"), (str, int, float, bool)) or c.get("actual") is None else str(c.get("actual"))[:120]}
                      for c in (item.get("checks") or [])[:40] if isinstance(c, dict) and c.get("status") in V2_RESPONSE_CHECKS]
            distance = item.get("targetDistanceMeters")
            evidence.append({"listingId": item["listingId"], "checks": checks,
                             "targetDistanceMeters": distance if isinstance(distance, (int, float)) and not isinstance(distance, bool) and isfinite(distance) and distance >= 0 else None})
        total = result.get("matchedTotal", result.get("total"))
        unknown = result.get("unknownTotal")
        return {"listingIds": ids, "total": total if isinstance(total, int) else len(ids),
                "unknownTotal": unknown if isinstance(unknown, int) else 0, "readAt": result.get("readAt"),
                "catalogVersion": result.get("catalogVersion"),
                "contextMissing": [str(v) for v in result.get("contextMissing") or []][:10], "evidence": evidence}

    async def value_opportunity(self, search: dict[str, Any], displayed_ids: list[str],
                                excluded_suggestion_ids: list[str] | None = None) -> dict[str, Any]:
        """A bounded above-budget pool, separate from main pagination and all main counts."""
        if not 1 <= len(displayed_ids) <= 4 or len(set(displayed_ids)) != len(displayed_ids):
            raise ListingToolError("Invalid value comparison selection")
        displayed_ids = [str(UUID(value)) for value in displayed_ids]
        excluded = [str(UUID(value)) for value in (excluded_suggestion_ids or [])][-40:]
        response = await self.gateway.request("POST", f"{self.base_path}/value-opportunity", request=self.request,
                                              json={"search": search, "displayedIds": displayed_ids,
                                                    "excludedSuggestionIds": excluded}, timeout=5.0)
        result = self._result(response)
        ids = result.get("candidateIds")
        if not isinstance(ids, list) or len(ids) > 8:
            raise ListingToolError("Invalid boundary candidate pool")
        try:
            ids = [str(UUID(value)) for value in ids]
        except (TypeError, ValueError, AttributeError):
            raise ListingToolError("Invalid boundary candidate ID") from None
        if len(set(ids)) != len(ids) or set(ids) & set(displayed_ids + excluded):
            raise ListingToolError("Core repeated an excluded boundary candidate")
        if ids:
            from decimal import Decimal, InvalidOperation
            try:
                ceiling, maximum = Decimal(str(result["budgetCeiling"])), Decimal(str(result["maxExtraMonthly"]))
                if (not ceiling.is_finite() or ceiling <= 0 or not maximum.is_finite() or not 0 < maximum <= 500000
                        or result.get("budgetField") not in {"rent.monthly_amount", "price_amount"}):
                    raise ValueError
            except (ValueError, InvalidOperation, KeyError):
                raise ListingToolError("Invalid boundary budget metadata") from None
        return {key: result.get(key) for key in ("budgetField", "budgetCeiling", "maxExtraMonthly", "readAt")} | {"candidateIds": ids}

    async def resolve_place(self, query: str) -> list[dict[str, Any]]:
        """Candidates from the stored POI catalog only. Never a provider call, never a guessed ID."""
        text = place_query(query)
        if not 2 <= len(text.strip()) <= 160:
            raise ListingToolError("A specific place name is required")
        response = await self.gateway.request("GET", f"{self.base_path}/nearby-places/search", request=self.request,
                                              params={"keyword": text.strip()})
        places = self._result(response).get("places", [])
        result = []
        for place in places[:10] if isinstance(places, list) else []:
            try:
                result.append({"placeId": str(UUID(str(place["id"]))), "name": str(place.get("name") or "")[:200],
                               "address": str(place.get("address") or "")[:300]})
            except (KeyError, TypeError, ValueError):
                continue
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
        media = [item for item in (result.get("media") or []) if isinstance(item, dict)]
        images = [item for item in media if item.get("mediaType") == "IMAGE"]
        images.sort(key=lambda item: (not item.get("cover"),
                                      item.get("sortOrder") if isinstance(item.get("sortOrder"), int) else 1 << 30))
        # One https cover for the comparison-table thumbnail; never storage IDs or non-https URLs.
        cover = next((item for item in images if isinstance(item.get("url"), str) and SAFE_COVER.match(item["url"])), None)
        if cover:
            safe["media"] = [{"mediaType": "IMAGE", "url": cover["url"], "cover": True,
                              "sortOrder": cover.get("sortOrder") if isinstance(cover.get("sortOrder"), int) else 0}]
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
    def _json(response: Any) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _result(response: Any) -> Any:
        if response.status_code != 200:
            raise ListingToolError(f"Core listing tool returned HTTP {response.status_code}",
                                   status_code=response.status_code)
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            raise ListingToolError("Invalid Core listing tool response")
        return data["result"]
