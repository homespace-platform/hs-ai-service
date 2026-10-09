# Realtime listing tools (backend)

The existing `POST /api/v1/ai/agent/ask` remains the single chat entry point. The
tenant's bearer token is forwarded by the AI service through Gateway to Core API.
The AI service never reads `homespace_core` directly and never generates SQL.

Flow:

1. Load up to eight prior messages from the caller-owned `conversationId` (when supplied).
2. The primary model uses native tool calling to choose a listing action or leaves the
   question with the existing RAG path. The configured fallback model is tried on a
   retryable primary-provider error.
3. Read Core's allowed field catalog from `GET /api/v1/public/listings/ai/fields`.
4. `search_listings` calls `POST /api/v1/public/listings/ai/search`, or a known-listing
   action reads `GET /api/v1/public/listings/ai/{listingId}`. Search returns IDs only;
   each result is re-read through the public-detail endpoint before answering.
5. Only published, active, unexpired listings can be returned. A failed tool request
   never falls back to RAG as a source of current listing facts.
6. The response is grounded in the current Core data. Parking answers are rendered
   deterministically from parking policy, capacity and the matching charge row.

Search body example:

```json
{
  "filters": [
    {"field": "category", "op": "eq", "value": "ROOM"},
    {"field": "room.max_occupants", "op": "gte", "value": "3"},
    {"field": "charge.MOTORBIKE_PARKING.included_in_rent", "op": "eq", "value": "true"}
  ],
  "sort": "price_asc",
  "limit": 5
}
```

All field names are provided by Core's catalog. The allowed operators are `eq`,
`ne`, `gt`, `gte`, `lt`, `lte`, and `contains`; Core rejects unknown fields and
invalid field/operator pairs instead of silently ignoring them. Multiple filters
are combined with AND. Detail includes common pricing/address data, the relevant
HOUSE/APARTMENT/ROOM object, charges, amenities, furnishings and viewing options.

The seed's street lines are examples rather than verified coordinates. Neither
the API nor the AI should claim a measured distance from a landmark. For a
live end-to-end test, restart Core API and AI service after deploying these
changes, then use an authenticated tenant session through Gateway.
