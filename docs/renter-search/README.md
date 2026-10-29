# AI renter search — handover

Scope: the requirements R01–R12 and acceptance cases T01–T37 of
`hs-infra/prompts/HOMESPACE_AI_RENTER_SEARCH_IMPLEMENTATION_AGENT_PROMPT.md`.

- [REQUIREMENTS_MATRIX.md](REQUIREMENTS_MATRIX.md): R01–R12 and T01–T37 → files → tests → results → what is still open.
- [FIELD_COVERAGE.md](FIELD_COVERAGE.md): all 304 catalog fields with source, operators, evidence and corpus IDs (generated).
- [CHAT_BEHAVIOR.md](CHAT_BEHAVIOR.md): when the assistant asks, searches, compares within scope, reports missing Nearby data or recommends at most three (Vietnamese, for manual testing).
- Corpus: `tests/eval/renter_corpus_v1.json` (200 turns: Q001–Q120 and C01–C10 × 8, mapped 1:1 to
  `hs-infra/documents/ask/AI_RENTER_MANUAL_ACCEPTANCE_QUESTIONS.md`). Per-ID results: `tests/eval/results/`.

**Status.** Implemented and tested offline (unit, contract, PostgreSQL on a temporary database).
**Real-model language understanding has not been accepted:** the paid eval is opt-in and has not
been run, so every live corpus status is `NOT_RUN`.

## Contracts and state

| Piece | Version | Where |
|---|---|---|
| Catalog `GET /public/listings/ai/catalog` | `version: 2`, `catalogVersion: fields-v2-<hash>` (changes when reference data changes) | `hs-listing-service/.../AiListingFieldCatalog.java` |
| Search `POST /public/listings/ai/search` | body `version: 2`; v1 `filters` still accepted for old callers | `AiListingSearchService.java`, `AiListingSearchRequest.java` |
| POI kinds | `nearbyKinds` in the catalog, source category IDs | `nearby/NearbyKinds.java`, `NearbyQueryService.groupPredicate` |
| `RenterSearchState` | `version: 1`, inside `listingContext` `version: 2` and at conversation level `renterSearchState` with a `revision` guard | `application/renter_search_state.py`, `repositories/conversation_repo.py` |
| Planner tools | OpenAI strict function schema generated from the catalog | `clients/listing_tool_planner.py` |
| Text guards | budget words, head count, categories, dates, preferences, unsupported needs | `application/renter_criteria.py`, `renter_dates.py` |

State is stored in the existing Mongo `ai_conversations` collection (no new storage). A conversation
saved before this change (only `listingIds`) keeps working: its references can be read, but no
criteria are reconstructed from old AI text.

## Configuration

New business settings are properties, validated at startup, with no fallback default:

| Service | Property | Shipped value | Meaning |
|---|---|---|---|
| AI | `listing.search-utc-offset` (`src/homespace_ai/application.properties`) | `+07:00` | Offset used to resolve "đầu tháng sau", "tuần sau" at request time |
| Core | `listing.ai.catalog-cache-seconds` (`hs-api-service/.../application.properties`) | `300` | Catalog cache window; 1–86400 |

Unchanged and still required: `AI_LISTING_MAX_RESULTS` / `listing.ai.max-results` (same value in both
services), `nearby.query-enabled`, `nearby.default-radius-m`. Keys stay in the backend `.env`; nothing
here reads or prints them. `nearby.sync-enabled`, the scan (2 km, `limit=100`, `ofs=0`, one combined
categorySet) and its fingerprint are not changed.

## Run the tests

### Offline AI (no network; a guard fails any non-loopback connection)

```powershell
cd D:\Workspace\homespace\hs-ai-service
uv run --no-sync pytest tests/unit tests/eval -q -p no:logging
uv run --no-sync python -m tests.eval.run_eval offline      # writes tests/eval/results/offline_guard_results.json
uv run --no-sync python -m tests.eval.field_matrix          # regenerates FIELD_COVERAGE.md after a catalog change
```

After a Core catalog change, refresh the fixture from the seeded catalog view and regenerate the matrix:

```powershell
cd D:\Workspace\homespace\hs-core-api
mvn -q -Pisolated-build -pl hs-listing-service -am test "-Dtest=AiCatalogContractTest" "-Dsurefire.failIfNoSpecifiedTests=false" "-Dhs.ai.catalog-fixture=D:\Workspace\homespace\hs-ai-service\tests\fixtures\core_catalog_v2.json"
```

### Core unit and contract tests

```powershell
cd D:\Workspace\homespace\hs-core-api
mvn -q -Pisolated-build -pl hs-listing-service -am test "-Dtest=Ai*Test,Nearby*Test,PublicListing*Test" "-Dsurefire.failIfNoSpecifiedTests=false"
```

The PostgreSQL tests below are skipped by this command unless their variables are set; a skip is not a pass.

### PostgreSQL integration (temporary container only)

The test uses `create-drop`, so it only runs when the URL points at a database named
`hs_ai_search_test`. Never point it at `hs-postgres` or any dev database.

```powershell
$name = "hs-ai-search-test-$PID"
docker run -d --rm --name $name -e POSTGRES_PASSWORD=ai-search-test-only -e POSTGRES_DB=hs_ai_search_test -p 127.0.0.1::5432 postgres:16-alpine
$port = (docker port $name 5432).Split(':')[-1]
$env:HS_AI_SEARCH_TEST_PG_URL = "jdbc:postgresql://127.0.0.1:$port/hs_ai_search_test"
cd D:\Workspace\homespace\hs-core-api
mvn -q -Pisolated-build -pl hs-listing-service -am test "-Dtest=AiListingSearchPostgresTest" "-Dsurefire.failIfNoSpecifiedTests=false" "-Dhs.seed.sql=D:\Workspace\homespace\hs-infra\postgres\seed\listings_seed_hcm.sql"
Remove-Item Env:HS_AI_SEARCH_TEST_PG_URL
docker rm -f $name
```

It loads the HCM seed (read-only use of the file) into the temporary schema, compares every predicate
with independent SQL, and asserts that search never writes scan jobs/runs/places/links. The Nearby
worker tests (`NearbyPostgresTest`) use their own guard; see the Nearby docs for their variables.

### Real-model eval (paid, opt-in, not run)

```powershell
cd D:\Workspace\homespace\hs-ai-service
$env:HS_AI_LIVE_EVAL = "1"
uv run --no-sync python -m tests.eval.run_eval live --confirm-paid --ids Q001,Q006,Q040
Remove-Item Env:HS_AI_LIVE_EVAL
```

It reads `OPENAI_API_KEY` and the model from the service `.env` through `Settings` (never printed),
sends only the customer text (no expected answer), calls only the planner and the deterministic layer
(no Core, TomTom or database), and writes `tests/eval/results/live_planner_results.json` with the model,
request time, catalog version, corpus hash and per-ID tool/arguments/problems. Multi-turn
conversations and cases that need displayed results stay `NOT_RUN` there; run them in the chat UI with
[CHAT_BEHAVIOR.md](CHAT_BEHAVIOR.md).
