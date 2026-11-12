# Traffic correction test matrix

Independent offline acceptance matrix for approved design v0.3. Synthetic geometries and recorded local OTP data are sufficient; no live paid API calls are allowed. Assertions concern observable contracts and independent numerical examples, not private implementation structure.

| Area | Cases and required result | Owner/tests |
| --- | --- | --- |
| Arithmetic D07/D24/D25/D31 | 480s baseline, half edge car=360s ->600s; fast car adds zero; uncovered base retained; known dwell once; repeated evaluation does not compound | Engine + regression |
| Matching | Whole and partial overlap, reverse travel rejected, crossing rejected, parallel ambiguity rejected, zero/missing shape or baseline skipped | Data geometry |
| Source choice D19 | Better geometry wins before fresher age; age resolves quality tie; exact tie deterministic; conflict visible; observations never summed | Engine + regression |
| Applicability D18/D34/D35 | Metro/rail/dedicated/segregated tram excluded; shared road eligible; unknown allowed with warning | Engine + integration |
| Index D22 | Existing disk index only; active graph mismatch/absent preserved source means no baseline; stop IDs distinct; loop stop_sequence distinct | Data graph + regression |
| Cache D05/D06 | Hit before 1800, miss at boundary; reads do not renew; bounded eviction; shared across searches; concurrent same-key single-flight; failed loader recoverable; geometry reuse independent of trip baseline | Data cache + regression |
| Timing D08/D15/D32/D37 | First departure preserved; exact reachable boarding preserved with shorter wait; minimum transfer counted once; any delayed frequency arrival replans; no homemade headway wait | Engine + integration |
| Tail D16/D27/D33 | Corrected alighting point/time and constraints; prefix preserved; old walk/wait/transit replaced; own new baseline; uncovered new roads keep baseline; no paid recall | Engine + integration |
| Recovery D17/D38/D40 | No-progress cycle stops; remaining transfer budget; expired deadline starts no calls; reachable old frequency tail unverified/incomplete; unreachable old boarding excluded with full untouched original debug | Engine + regression |
| Quality D28/D34/D39 | 79.99% incomplete; 80% sufficient; weighted by distance; added=0 can still cover; denominator zero ->not_applicable/null; shared result limit; sorting arrival/transfers/walk | Integration + regression |
| Providers D02/D03/D04 | At most one 2GIS driving fastest jam detailed+alternatives; at most one realtime Yandex <=50 points no optimize/future departure; alternatives/steps retained; path duration not duplicated across geometry | Data provider + integration |
| Waypoints D20/D23/D29 | All current candidates prioritized; branches/shared boundaries can be anchors; repeats count; connector not fabricated bus edge; then BFS1/BFS2 with line count/proximity; fewer than 50 permitted | Data graph |
| Errors and safety D13/D30 | Dummy no provider/network calls; real error no synthetic routes; quota/error no retry; full structured debug without flag; exceptions and secrets redacted | Integration |
| Budget | Monotonic 15s includes OTP/provider/recovery; remaining timeout passed; fake clock makes budget tests deterministic; bounded blocking tests use releasable events | Integration + regression |

Regression fixtures are deliberately small and self-describing. Test files install a socket-connect guard so missing mocks fail locally rather than consume quota. The current offline baseline is 332 passing tests (verified 12 November 2026). That baseline is not evidence of correction accuracy; new numerical and flow assertions are required.


## Independent acceptance results

At the 12 November 2026 implementation checkpoint, `test_traffic_integration.py` and `test_traffic_regressions.py` contribute **70 passing cases** (30 integration, 40 regression). They exercise the production API composition with a real temporary GTFS disk index and actual Yandex/2GIS adapters behind `httpx.MockTransport`, in addition to deterministic engine examples. The API test proves a 1200-second indexed trip becomes 1500 seconds from a road step, and that a repeated user search consumes no additional provider calls. A sentinel key is present only in mocked provider environment configuration and is absent from response diagnostics.

The tests independently reproduced a dropped initial OTP wait; the engine owner fixed it and the regression now preserves both route start and first boarding. Indexed graph tests prohibit CSV reopening and unfiltered stop_times reads, retain the active source after the latest pointer changes, reject graph/source mismatch, select mixed frequency windows and require stop_sequence for ambiguous loop passages. Timeout tests cover a real blocking initial planner and a concurrent cache waiter without canceling the loader.

Fresh review added six passing cases in `test_traffic_review.py`, including exact and frequency two-stage recovery with one collection, global geometric order, timestamp-timezone invariance, large-shape complexity, and BFS-neighbor ranking under the 50-point quota. A physical ETA accuracy claim still requires independent traffic ground truth, which these offline fixtures do not provide. The optional curated directed-lane layer must come from real evidence; these tests do not fabricate a complete official city-wide layer.


## Decision-to-test traceability

These names refer to functions in `backend/tests/`; parameterized cases are included in the independent count above. Owner and fresh-review suites supplement the 70 independent cases and are included in the final counts below.

| Decisions | Concrete checks |
| --- | --- |
| D01, D36 | `test_otp.py` candidate normalization and constraints; `test_api_production_composition_reads_active_index_and_reuses_road_cache`; engine preserves OTP provenance. Supplemental per-mode breadth remains dependent on OTP output. |
| D02–D04, D13–D14 | `test_provider_one_call_current_parameters_and_budget`, `test_api_dummy_and_traffic_off_never_call_road_providers`, `test_provider_errors_are_not_retried_and_raw_exception_secret_is_not_debugged`, production-composition cache test. No live experiment is part of acceptance. |
| D05–D06 | `test_cache_reads_do_not_extend_thirty_minute_ttl`, `test_cache_respects_observation_expiry_instead_of_insertion_time`, `test_cache_evicts_least_recently_used_and_bounds_bytes`, `test_cache_same_key_concurrent_requests_share_one_load`, `test_singleflight_waiter_honors_own_timeout_without_canceling_owner`, `test_reusing_observation_for_different_trips_recomputes_own_baseline`. |
| D07, D24–D25, D31 | `test_approved_half_edge_example_preserves_uncovered_movement`, `test_partial_road_step_time_is_allocated_once_by_length`, `test_dwell_is_not_part_of_moving_baseline_or_road_increment`, faster-car parameterized tests, repeated-evaluation test. |
| D08–D09, D15–D17, D32–D33, D37–D38, D40 | `test_exact_transfer_absorbs_delay_in_wait_and_keeps_boarding`, `test_delayed_reachable_frequency_tail_requires_recovery_and_can_fallback_unverified`, `test_successful_tail_replaces_old_walk_wait_and_rides_and_preserves_prefix`, `test_successful_frequency_repair_does_not_request_same_boundary_again`, `test_unreachable_unrepaired_candidate_excluded_with_full_original`, initial-wait test, API deadline504 test. Owner `test_transfer_slack_is_not_added_twice` and `test_transfer_minimum_parent_and_specific_trip_override` cover OTP/GTFS transfer minima. |
| D10, D22 | `test_graph_uses_existing_selected_trip_index_without_csv_or_full_stop_times_scan`, `test_active_graph_rejects_version_mismatch_and_never_uses_latest`, `test_loop_stop_ids_require_sequence_instead_of_silently_selecting_first_passage`, owner `test_same_edge_incompatible_loaded_trip_shapes_are_ambiguous`. |
| D11, D18, D35 | `test_non_applicable_edges_are_removed_from_denominator`, `test_unknown_road_status_allows_correction_with_warning`, owner `test_curated_road_status_provenance`. Owner `test_partial_directed_lane_excludes_only_half_with_full_length_time_baseline` verifies partial applicability; overlapping spans are rejected atomically. |
| D12, D26, D30 | `test_observation_debug_distinguishes_received_from_applied_when_added_zero`, `test_source_conflict_selects_geometry_before_freshness_and_never_sums`, `test_equal_geometry_chooses_fresher_observation_and_exact_tie_is_stable`, `test_traffic_diagnostics_contract.py` age/horizon and rejected/excluded contracts. |
| D19 | Source-choice tests above plus `test_overlapping_observations_and_alternatives_do_not_double_cover`; source conflict IDs and stable tie ordering asserted. |
| D20, D23, D29 | Owner `test_waypoint_quota_counts_connectors_and_repeated_stops`, `test_closed_loop_keeps_intermediate_anchors`, `test_long_candidate_corridors_share_anchor_quota`, `test_paid_collection_diagnostic_records_original_ab_and_connectors`. Fresh review `test_neighborhood_quota_prioritizes_depth_lines_then_proximity` verifies competing-neighbor quota ranking. |
| D27 | Successful-tail test above returns uncovered replacement at its own baseline and incomplete quality; provider budgets prevent paid recall. |
| D28, D34, D39 | `test_quality_threshold_is_distance_based_with_null_non_applicable`, `test_shared_result_limit_prefers_sufficient_over_faster_partial_and_zero_eligible`, `test_grouping_ties_use_arrival_then_transfers_then_walking`, `test_missing_geometry_part_remains_eligible_in_distance_denominator`, `test_unverified_tail_overrides_even_full_road_coverage`. |

Latest independent rerun: **70 passed in 0.98 seconds**. The API deadline regression now asserts HTTP 504 without a synthetic route; the engine deadline assertion expects the normal `ProviderError` contract. Final root verification: **456 backend tests passed** (124 above the 332-test baseline), **103 other CI Python tests passed**, and **14 frontend tests passed**, for **573 automated tests total**. Backend runtime 9.58 seconds (one warning), other Python runtime 4.64 seconds. Frontend build succeeded, `git diff --check` is clean, and the generated OpenAPI snapshot matches.


The fresh-review `test_two_recoveries_correct_each_new_ride_once_with_one_collection` is parameterized for scheduled and frequency service and closes the multi-stage tail recovery coverage gap. No live paid calls and no commits were made during this implementation or its verification.

## Final verification checkpoint

After the lane-data, exact recovery-duplicate and cache-order fixes, the full backend suite passed **509 tests in 10.60 seconds**, with one warning: **177 above the original 332-test baseline**. The OpenAPI snapshot matches and `git diff --check` is clean. Previously verified unchanged components contribute 103 other CI Python tests and 14 frontend tests, for **626 automated tests total**; the frontend build succeeded. The 35 independent lane-acceptance cases, 8 builder cases and 41-case acceptance/recovery subset are overlapping views within that backend total, not additional tests to sum.
