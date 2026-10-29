# R01–R12 and T01–T37: files, tests, results, open items

Results are from the runs on 2026-10-29: AI `pytest tests/unit tests/eval` 579 passed; Core
`hs-listing-service` `Ai*Test,Nearby*Test` all passed (Postgres tests skipped in that run) and `hs-api-service` `NearbyPlacesControllerTest` 3/3;
`AiListingSearchPostgresTest` passed on a temporary `postgres:16-alpine` container named
`hs_ai_search_test`; `NearbyPostgresTest` 35/35 passed on its own temporary container. The paid
real-model eval was **not run**.

Paths: AI = `hs-ai-service/src/homespace_ai/`, AI tests = `hs-ai-service/tests/`, Core =
`hs-core-api/hs-listing-service/src/main/java/com/hs/listing/`, Core tests = `.../src/test/java/com/hs/listing/`.

## Requirements

| ID | What changed | Main files | Tests | Result | Still open |
|---|---|---|---|---|---|
| R01 Catalog | Versioned catalog (`version 2`, `catalogVersion` hash) with label, kind, operators, unit, categories, enum code+label, source, null meaning, role (FILTER/COMPUTED/DISPLAY_ONLY), required context, sorts, POI kinds, unsupported needs. Codes come from reference tables; cache with a configured window and version. Structured 400 `{reason, field, allowed}` for an unknown field, operator or value. | Core `service/AiListingFieldCatalog.java`, `AiSearchValidationException.java`, `config/ListingReferenceSeeds.java`; AI `application/listing_catalog.py` | `AiCatalogContractTest`; `AiListingSearchV2Test` (`newReferenceItemBecomesSearchableWithoutCodeChangeAndChangesTheVersion`, `liveCatalogRequiresAConfiguredCacheWindow`, `invalidEnumIsAStructuredErrorNotAnEmptyResult`); `AiListingSearchServiceTest.rejectsUnknownFieldAndOperator…`; AI `test_listing_tool_planner` (catalog errors), `tests/eval/test_corpus.py` (every expected field exists) | Pass | Vietnamese synonyms are only the catalog label; other phrasings depend on the model and the text guards |
| R02 Field coverage | Integer policies compared as numbers with a guarded cast; RANGE_HOOD/MICROWAVE/SHOE_CABINET/WATER_TANK_PUMP and every furnishing code filterable (quantity, total quantity, condition); permission enum with APPROVAL_REQUIRED; mappings for elevator/WC/access/balcony/occupants; highlights display-only; gym near ≠ gym in building (`NEARBY_SPORTS`) | Core `AiListingSearchService.java`, `AiListingFieldCatalog.java`; AI `application/renter_criteria.py` | [FIELD_COVERAGE.md](FIELD_COVERAGE.md); T17–T21 below | Pass | See the per-source evidence level in FIELD_COVERAGE: apartment/house/media/viewing fields have no field-specific SQL assertion |
| R03 Plan outcomes | SEARCH / CLARIFY / DETAIL / COMPARE / NEARBY / RAG. Clarify carries the missing field, question, options and the criteria already understood. Location gate (ward/city, unique target or explicit nationwide); no account address. A unique IUH is enough. "Phòng" adds no category, "phòng trọ" is ROOM. Vague words become preferences or limitations. Same-turn contradictions ask. | AI `application/use_cases/listing_ask.py`, `clients/listing_tool_planner.py`, `application/renter_criteria.py` | T01, T10, T35, T37; `test_listing_tool_planner` (sanitized clarification, generic "phòng") | Pass | Real-model choice between SEARCH and CLARIFY is unmeasured (eval NOT_RUN) |
| R04 State | `RenterSearchState` v1 (typed hard/preference constraints with source turn, location, target, nearby, context, unsupported, pending, shown IDs + fingerprint, revision) in `ai_conversations`; merge keeps omitted criteria, removal needs explicit words, reset needs reset words; reading a fact or a hypothetical never edits state; incompatible fields after a category change ask; optimistic revision guard | AI `application/renter_search_state.py`, `repositories/conversation_repo.py`, `api/v1/agent_ask.py`, `api/v1/guest_agent.py` | T02, T06–T08, T33; `test_renter_search_state.py` (27 tests) | Pass | — |
| R05 No relaxation | One search per turn; empty result lists the kept criteria, unknown count and missing context and asks what to change; only the removed criterion is dropped; preferences never filter; the relaxation module was deleted | AI `listing_ask.py` (`_no_results`), deleted `application/budget_stretch.py` and its test | T05, T06; `test_listing_location_and_budget` (no-relax empty), `test_listing_presentation` | Pass | — |
| R06 Named landmark | Landmark resolved through the stored POI warehouse (accent-insensitive, managed aliases); several campuses become choices; the chosen ID is re-validated by Core (`TARGET_UNAVAILABLE`); filter and sort by straight-line distance from coordinates, no link needed; keyword equal to the landmark is dropped | Core `nearby/NearbyQueryService.java` (`targetPredicate`), `AiListingSearchService.java`; AI `data/nearby_place_aliases.json`, `tools/listings.py` (`resolve_place`) | T09–T12, T31; `NearbyPostgresTest.exactTargetUsesCoordinatesEvenWhenItIsNotTheGenericNearestSchool`; PG named target 1 km and 5 km | Pass | Seed addresses are demo data: distances prove the logic, not real geography |
| R07 Nearby coverage | Several independent predicates; three-valued group/kind predicate (link → TRUE, complete coverage → FALSE, else UNKNOWN, counted in `unknownTotal`); specific kinds by verified source category (hospital ≠ pharmacy, supermarket ≠ convenience store); SPORTS not collected → limitation | Core `NearbyQueryService.groupPredicate`, `nearby/NearbyKinds.java`; AI `application/nearby_listing_advice.py` | T13, T14, T34, T36; PG convenience store / supermarket UNKNOWN / hospital vs pharmacy-only | Pass | The seed has no supermarket POI and no COMPLETE coverage; the FALSE branch is covered by `NearbyPostgresTest`, not by AI search on the seed |
| R08 Compiler | Typed AND/OR tree with depth/size limits; same-row EXISTS for charges, policies and furnishing; furnishing total quantity over matching rows; ordinal condition ranks; null is unknown | Core `AiListingSearchService.java`, `dto/request/AiListingSearchRequest.java` | T16, T18; `AiListingSearchV2Test` (`orGroups…`, `conditionsOnOneChargeType…`, `furnishingQuantityAndCondition…`, `treeDepthAndSizeAreBounded`); PG OR and BED total | Pass | T18's four condition combinations are asserted on SQL shape; PG truth covers total quantity only |
| R09 Money, cost, dates | `rent.monthly_amount` by price unit × occupants (UNKNOWN without occupants); `cost.fixed_monthly_total` / `cost.estimated_monthly_total` with context and no double counting; dưới `<`, tối đa `<=`, ít nhất `>=`, tầm/khoảng = preference; rưỡi, 4tr5, per-person × head count; dates from the request clock and configured offset | Core `AiListingSearchService.java`; AI `renter_criteria.py`, `renter_dates.py`, `core/business_properties.py` | T04, T22–T24; `test_listing_tool_planner` budget-word table (including fee amounts that must not become a rent bound); PG per-person rent | Pass | Guards don't parse word numbers ("một triệu rưỡi", "ba tháng", "hai đứa mình"); the model must. The seed has no PERSON_MONTH listing (that path is covered by unit tests only) |
| R10 Scope advice, top 3 | "Trong các phòng trên" uses `scopeIds`; changed criteria are re-checked in Core before ranking; at most three recommendations, fewer if fewer qualify; browse size follows the requested count up to the cap | AI `nearby_listing_advice.py`, `listing_ask.py`; Core scope/exclude | T15, T26; PG scope/exclude | Pass | Ranking uses only stated needs and evidence; no learned quality score |
| R11 Evidence, payload | Core builds per-condition evidence (requested, actual, MATCH/NOT_MATCH/UNKNOWN, target distance) for returned IDs; bounded preview plus evidence; detail reads limited to the page; hidden/expired listings are skipped; listing text is untrusted | Core `dto/response/AiListingSearchResponse.java`, `AiListingSearchService.java`; AI `listing_ask.py`, `application/listing_evidence.py` | T25, T28, T30; `AiListingSearchV2Test.evidenceReportsEveryConditionForReturnedListings`; PG MATCH evidence | Pass | No index was proposed; query timing is logged (`latency_ms`) but not benchmarked |
| R12 Paging, diagnostics | `excludeIds` bound to the criteria fingerprint (reset on change); distinct outcomes (`PLAN_REJECTED` with reason/field, `NO_RESULTS`, `TARGET_UNAVAILABLE`, `NEARBY_UNAVAILABLE`, tool failure); logs carry request ID, outcome, tool, filter/result counts, payload bytes, latency, no customer text; support questions stay on RAG | AI `listing_ask.py`, `tools/listings.py`; Core controller | T27, T29, T30, T32; `test_listing_ask.test_search_logs_are_correlated_and_measured_without_customer_text` | Pass | — |

## Acceptance cases

| ID | Test(s) | Result |
|---|---|---|
| T01 | `test_listing_ask.test_t01_elevator_only_asks_for_area_and_never_searches` | Pass |
| T02 | `test_t02_short_location_reply_merges_with_pending_elevator_need` | Pass |
| T03 | `test_t03_complete_request_sends_every_hard_filter_and_no_keyword_substitute`; PG ward/category/price | Pass |
| T04 | `test_t04_budget_boundaries_follow_the_words_not_the_model` (dưới/tối đa/không quá/ít nhất) | Pass |
| T05 | `test_t05_empty_hard_search_is_reported_once_never_relaxed` | Pass |
| T06 | `test_t06_removing_elevator_keeps_budget_people_and_area` | Pass |
| T07 | `test_t07_state_survives_more_than_eight_messages_and_only_budget_changes`; `test_renter_search_state.test_t07_latest_state_reads_the_full_transcript_not_the_prompt_window` | Pass |
| T08 | `test_t08_legacy_listing_ids_only_conversation…`; `test_t08_conversation_level_state_wins_and_legacy_v1_has_none`; `test_t08_revision_guard_never_lets_a_slower_request_overwrite_newer_criteria` | Pass |
| T09 | `test_t09_named_landmark_uses_target_coordinates_not_title_keyword`; `AiListingSearchV2Test.namedTargetUsesTheBoundTargetDistanceAndCanSortByIt` | Pass |
| T10 | `test_t10_two_campuses_ask_then_short_choice_keeps_budget_and_people` | Pass |
| T11 | `NearbyPostgresTest.exactTargetUsesCoordinatesEvenWhenItIsNotTheGenericNearestSchool` | Pass |
| T12 | PG named target (35 listings within 1 km by coordinates, no link required) | Pass |
| T13 | `test_t13_target_and_supermarket_are_two_independent_predicates`; `AiListingSearchV2Test.severalNearbyPredicatesAreAllApplied` | Pass |
| T14 | `test_nearby_listing_advice` (`test_five_km_request_cannot_be_silently_answered…`, `test_uncollected_sports_is_unknown…`, `test_limited_sample_with_no_school_evidence…`); `NearbyPostgresTest.completeThreeKmNeverClaimsFiveKmCoverageAndFiltersBeforeLimit`; PG supermarket unknown=300 | Pass |
| T15 | `test_t15_hard_criteria_are_rechecked_in_scope_before_ranking_top_three`, `test_t15_no_eligible_room_in_scope_is_no_results_not_a_nearby_pick` | Pass |
| T16 | `test_t16_room_or_apartment_for_three_is_two_categories_with_capacity`; PG OR groups | Pass |
| T17 | `AiListingSearchV2Test.integerPolicyUsesGuardedCastOnTheSamePolicyRow`; PG `policy.depositRefundDays.value <= 7` | Pass |
| T18 | `AiListingSearchV2Test.furnishingQuantityAndConditionShareRowsAndUseOrdinalRanks` (SQL shape); PG `furnishing.BED.total_quantity` | Pass (see R08 open item) |
| T19 | `AiListingSearchV2Test.integerPoliciesAndEveryFurnishingCodeArePublishedWithMetadata` | Pass |
| T20 | `AiListingSearchServiceTest.petPermissionUsesPolicyValueAndCannotMatchAnotherPoliciesRow`; V2 enum list with APPROVAL_REQUIRED | Pass |
| T21 | `test_listing_tool_planner.test_t21_gym_in_the_building_is_an_amenity_but_a_gym_nearby_is_an_unknown_limitation`, `test_t21_uncollected_sports_group…`; balcony tests in `test_listing_tool_planner`/`test_renter_search_state` | Pass |
| T22 | `test_t22_total_monthly_cost_uses_computed_field_and_renter_context`; `AiListingSearchV2Test.costTotalsMirrorTheEstimateRulesAndRejectInvalidContext` | Pass |
| T23 | `test_t23_relative_move_in_date_and_lease_use_the_request_clock`; `test_t23_dates_use_a_fixed_aware_clock_and_configured_offset` | Pass |
| T24 | `AiListingSearchV2Test.perPersonRentWithoutOccupantsIsUnknownAndReported`; PG rent with/without occupants | Pass (no PERSON_MONTH rows in the seed) |
| T25 | `test_listing_location_and_budget.test_thousand_matches_never_cause_more_than_five_detail_reads`; `test_listing_result_counts`; PG LIMIT after total | Pass |
| T26 | `test_t26_t27_more_results_exclude_shown_and_changed_criteria_restart_paging`; `test_nearby_listing_advice` (at most three) | Pass |
| T27 | `test_t26_t27_…`; `test_renter_search_state.test_t27_paging_is_bound_to_the_criteria_fingerprint` | Pass |
| T28 | `test_t28_listing_hidden_between_search_and_detail_is_skipped_not_fatal` | Pass |
| T29 | `test_t29_core_rejection_is_a_diagnosis_not_no_results_and_is_not_retried`, `test_t29_invalid_enum_from_model_is_rejected_before_any_search`; PG invalid enum | Pass |
| T30 | `test_t30_support_question_stays_rag_and_listing_text_is_untrusted` | Pass |
| T31 | PG named target at 5 km (300 listings; 30 beyond 2 km with no link to the target); `test_five_km_request…` for group coverage honesty | Pass |
| T32 | `test_listing_tool_planner.test_t32_every_planner_argument_compiles_to_catalog_fields_and_operators`; `test_agent_listing_route` (catalog → v2 search → persisted state) | Pass |
| T33 | `test_t33_reading_a_fact_of_room_two_never_adds_a_filter` | Pass |
| T34 | `test_nearby_listing_advice.test_limited_hundred_result_sample_can_compare_positive_evidence_and_only_recommend_three`; PG convenience store EXISTS under PARTIAL coverage (263 listings) | Pass |
| T35 | `test_t35_clear_scope_searches_without_a_persona_or_budget_questionnaire` | Pass |
| T36 | PG: supermarket request matches none although convenience stores exist; hospital request never matches the 32 pharmacy-only listings | Pass |
| T37 | `test_t37_contradictory_range_asks_and_keeps_other_criteria` | Pass |

## Natural-language corpus

`tests/eval/renter_corpus_v1.json` holds 200 turns whose inputs match the manual document word for
word (checked by a test). Offline, 44 single-turn cases have deterministic guard expectations and all
pass (`tests/eval/results/offline_guard_results.json`). **All 200 live statuses are `NOT_RUN`.**
Cases Q075 and Q092 are marked `MISSING_FIXTURE` notes for the live run (the seed has no matching
data), and Q116 carries a note on a guard gap the model must cover.
