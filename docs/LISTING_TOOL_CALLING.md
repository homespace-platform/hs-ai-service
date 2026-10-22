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
   Application code resolves city/ward names, Vietnamese without accents and city aliases
   such as HCM/TP.HCM/Sài Gòn against the packaged Vietmap **admin_new** snapshot.
   This is the same JSON pair used by Next.js; its 3,321 wards are never sent to the model.
   Ambiguous names/legacy districts require clarification, not an invented ward code.
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

All explicit criteria are pushed into the Core query: location IDs, category, price,
area, occupants and requested amenities/furnishings. SQL applies `WHERE` **before**
`ORDER BY ... LIMIT`. A ward with 1,000 matches returns only a total count and at most
the requested number of IDs, never 1,000 listing objects. The default is **5**, not a
hard cap: "tìm 10 tin" / "cho tôi xem mười phòng" sets `limit=10`, even if the model
accidentally returns its old default. Bedrooms, WC, budget, occupants and ordinals
such as "phòng 10" are not interpreted as result counts. Core and the AI client enforce
a configurable per-turn maximum (default **20**, absolute ceiling **50**).
Broad requests show a small preview and ask
for more preferences; they do not invent extra criteria.

For new searches, only the requested page of public details is read and converted to
`search_preview` evidence capped at **4,096 UTF-8 bytes/listing** (10 results: at most
40,960 bytes; with the default maximum 20: at most 81,920 bytes of listing evidence).
Descriptions, handover notes and oversized collections are not passed wholesale to
the model. `omittedFields` marks unread/omitted data as unknown. Explicit detail and
follow-up tools still re-read full current public facts within the configured limit.
Cards and stored references preserve the entire returned page, including "phòng 10";
there is no hidden four/five-card truncation. Requests above the cap and shortages
of matching listings are explicitly disclosed in the answer.
Browse queries can use the deterministic answer and skip answer generation entirely.
No-results relaxation is capped at 2 additional SQL calls and never drops location,
budget, area or category. Any softened preference remains disclosed in the answer.

All field names are provided by Core's catalog. The allowed operators are `eq`,
`ne`, `gt`, `gte`, `lt`, `lte`, and `contains`; Core rejects unknown fields and
invalid field/operator pairs instead of silently ignoring them. Multiple filters
are combined with AND. Detail includes common pricing/address data, the relevant
HOUSE/APARTMENT/ROOM object, charges, amenities, furnishings and viewing options.

The seed's street lines are examples rather than verified coordinates. Neither
the API nor the AI should claim a measured distance from a landmark. For a
live end-to-end test, restart Core API and AI service after deploying these
changes, then use an authenticated tenant session through Gateway.

## Refresh the resolver data

After `npm run sync:data` in `hs-web-app`, run from `hs-ai-service`:

```powershell
.\.venv\Scripts\python.exe scripts/sync_location_catalog.py
.\.venv\Scripts\python.exe scripts/sync_location_catalog.py --check
```

Commit the generated `src/homespace_ai/data/vietmap/` files together with the frontend
snapshot. The compact catalog, upstream attribution/license and both source hashes
are packaged in the AI wheel/image; production does not read a sibling frontend
checkout or call GitHub/Vietmap for every chat message. Restart AI after syncing
and restart Core after deploying the stricter search limit. No DB reset/reseed is needed.

## Configure the result page maximum

Use the **same value** for both services, then restart them:

```dotenv
# AI service
AI_LISTING_MAX_RESULTS=20
# Core API (or application property listing.ai.max-results=20)
LISTING_AI_MAX_RESULTS=20
```

Values 1–50 are accepted. This is the per-turn safety ceiling, not the default
number displayed: unspecified requests still use 5 (or the ceiling if below 5).
The frontend accepts up to 50 verified references and continues lazy loading cards
with bounded request concurrency. Raising a page maximum does not fetch the whole ward.
