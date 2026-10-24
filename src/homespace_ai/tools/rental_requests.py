"""Read-only tool: PENDING rental requests of one listing, read as the calling landlord.

The AI service never opens the Core database. The Gateway forwards the landlord's JWT and Core only returns the
requests owned by that landlord, so a non-owner simply sees nothing.
"""

from typing import Any
from uuid import UUID

from fastapi import Request

from homespace_ai.clients.gateway import GatewayApiClient

PENDING_PAGE_SIZE = 50  # Core caps a page at 50
# Contact data and internal user IDs are never needed to compare requests, so they never reach the model.
SAFE_FIELDS = ("id", "listingId", "listingTitle", "listingAddress", "listingPrice", "renterName", "moveInDate",
               "leaseMonths", "occupantCount", "motorbikeCount", "carCount", "monthlyRentPrice",
               "effectiveMonthlyRent", "estimatedMonthlyTotal", "depositAmount", "estimatedInitialTotal",
               "estimatedLeaseTotal", "renterNote", "status", "createdAt", "respondBy")


class RentalRequestToolError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RentalRequestTools:
    def __init__(self, gateway: GatewayApiClient, request: Request) -> None:
        self.gateway = gateway
        self.request = request

    async def pending_for_listing(self, listing_id: str) -> list[dict[str, Any]]:
        try:
            safe_id = str(UUID(str(listing_id)))
        except ValueError as error:
            raise RentalRequestToolError("Invalid listing ID", status_code=400) from error
        response = await self.gateway.request(
            "GET", "/api/v1/rental-requests/owner", request=self.request,
            params={"listingId": safe_id, "status": "PENDING", "page": "1", "size": str(PENDING_PAGE_SIZE)})
        if response.status_code != 200:
            raise RentalRequestToolError(f"Core rental request tool returned HTTP {response.status_code}",
                                         status_code=response.status_code)
        data = response.json()
        rows = data.get("result") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise RentalRequestToolError("Invalid Core rental request response")
        return [{key: row.get(key) for key in SAFE_FIELDS if key in row}
                for row in rows if isinstance(row, dict) and row.get("status") == "PENDING"]
