# Realtime listing tools (backend)

The existing `POST /api/v1/ai/agent/ask` remains the single chat entry point. The
tenant's bearer token is forwarded by the AI service through Gateway to Core API.
The AI service never reads `homespace_core` directly and never generates SQL.

Flow:

1. Load up to eight prior messages from the caller-owned `conversationId` (when supplied) for
   the model, and the latest `RenterSearchState` from the whole conversation (see
   [renter-search/README.md](renter-search/README.md)). The state, not the eight-message
   window, carries the customer's criteria.
2. Reading a known reference ("phòng 2 có thang máy không?") skips the planner and never
   changes the criteria. Otherwise the model uses native strict tool calling
   (`search_listings`, `ask_clarification`, `get_listing_detail(s)`,
   `get_listing_nearby_places`) or leaves the question with the existing RAG path.
3. Read Core's versioned catalog from `GET /api/v1/public/listings/ai/catalog`
   (fields with kind/operators/unit/categories/enum labels/source/null meaning, context
   fields, sorts, nearby kinds, unsupported needs). Planner arguments are validated against
   it; an unknown field/operator/enum is rejected, never dropped. Deterministic text guards
   then fix budget words ("dưới/tối đa/ít nhất", "4tr5", "rưỡi", per person), head count,
   category words, dates and preferences. Application code resolves city/ward names,
   Vietnamese without accents and city aliases such as HCM/TP.HCM/Sài Gòn against the packaged
   Vietmap **admin_new** snapshot; ambiguous names/legacy districts require clarification.
   A named landmark is resolved through `GET /public/listings/ai/nearby-places/search`;
   several campuses are offered as choices, never guessed.
4. The merged state is compiled to `POST /api/v1/public/listings/ai/search` with
   `version: 2` (typed AND/OR tree, context, named-target and POI-kind predicates,
   `scopeIds`/`excludeIds`, evidence). Search returns IDs, totals, `unknownTotal`,
   `contextMissing` and per-condition evidence; each result is re-read through
   `GET /api/v1/public/listings/ai/{listingId}` before answering. The legacy
   `GET /ai/fields` and v1 search body stay available for older callers.
5. Only published, active, unexpired listings can be returned. A failed tool request
   never falls back to RAG as a source of current listing facts.
6. The response is grounded in the current Core data. Parking answers are rendered
   deterministically from parking policy, capacity and the matching charge row.

Search body example:

```json
{
  "version": 2,
  "filters": [],
  "where": {"all": [
    {"field": "rent.monthly_amount", "op": "lte", "value": "4000000"},
    {"field": "occupancy.max_occupants", "op": "gte", "value": 3},
    {"field": "ward_id", "op": "eq", "value": "24588"},
    {"any": [{"all": [{"field": "category", "op": "eq", "value": "ROOM"}]},
             {"all": [{"field": "category", "op": "eq", "value": "APARTMENT"}]}]}
  ]},
  "nearbyAll": [{"placeId": "<resolved IUH place id>", "radiusMeters": 2000},
                {"kind": "SUPERMARKET", "radiusMeters": 500}],
  "context": {"occupants": 3},
  "sort": "target_distance",
  "limit": 5,
  "excludeIds": [],
  "evidence": true
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
An empty result is never relaxed: there is exactly one search per turn, and the answer
lists the criteria kept, the listings whose data is unknown (`unknownTotal`), missing
cost context, and asks which criterion to change. Only a criterion the customer removes
is dropped. Preferences ("ưu tiên", "tầm/khoảng") are recorded but never filter. Scoped
advice ("trong các phòng trên…") re-checks every hard criterion in Core with `scopeIds`
before ranking at most three.

All field names are provided by Core's catalog. The allowed operators are `eq`,
`ne`, `gt`, `gte`, `lt`, `lte`, and `contains`, restricted per field; Core rejects unknown
fields, invalid field/operator pairs and invalid values with a structured 400
`{reason, field, allowed}` instead of an empty result. Detail includes common
pricing/address data, the relevant HOUSE/APARTMENT/ROOM object, charges, amenities,
furnishings and viewing options.

Distances are straight-line from stored listing and POI coordinates (never travel time).
The seed's addresses are demo data, so its distances prove the logic, not real-world
geography. Chat never calls TomTom; Nearby data comes only from the stored scans. For a
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
