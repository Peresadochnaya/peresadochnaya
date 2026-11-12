# Traffic Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct OTP surface-transit candidates using current road observations while preserving schedule semantics and exposing uncertainty.

**Architecture:** Read the existing active-graph GTFS disk index; derive compact directed stop edges and independent road observations. A search-scoped orchestrator applies positive movement corrections, rebuilds temporal chains and repairs tails before distance-based grouping. Provider adapters and a bounded shared cache isolate paid access from correction arithmetic.

**Tech Stack:** Python, FastAPI/Pydantic, SQLite GTFS index, httpx, pytest.

**Spec:** `docs/superpowers/specs/2026-11-12-traffic-correction-design.md` (v0.3, approved).

## Global Constraints

- Freshness is 1800 seconds from acquisition, never renewed by reads; search deadline is 15 seconds.
- At most one Yandex call (50 points) and one 2GIS automotive calculation with alternatives per search; no paid retries.
- Dummy mode performs no paid access; real failures never become demos.
- Reuse the merged GTFS disk index; active graph version and source GTFS must agree.
- Known dwell remains separate and counted once; correction is positive only and always uses original movement time.
- No commits until the user approves the commit title.
- Full diagnostics always returned; credentials, authorization headers and secret URLs excluded.

## Review Focus

- Loops revisit an edge with different stop sequences: correct each passage independently without charging observations twice.
- A delayed frequency transfer remains reachable: replan anyway; deadline fallback must be explicitly unverified and incomplete.
- An unreachable transfer cannot be repaired: exclude it and preserve the complete original route in debug.
- No eligible distance: return null ratio and not_applicable, and share the same max_results budget with sufficient candidates.
- GTFS changes while road observations remain fresh: reject incompatible temporal baselines without treating road TTL as a schedule version.

---

### Task 1: Active graph, geometry and observation cache

**Files:** Create `backend/app/traffic_graph.py`, `backend/app/traffic_geometry.py`, `backend/app/traffic_cache.py`, `backend/app/traffic_data.py`; use `backend/app/gtfs_index.py`.

**Ownership:** traffic_data_impl owns graph, geometry, cache, data and provider adapters. traffic_engine_impl owns engine, timing, OTP and API wiring. traffic_test_spec owns this plan, test matrix, integration/regression tests and their compact fixtures.

**Interfaces:** Graph loader resolves the active graph/source pair; `graph.trip_segments(trip_id, service_day=None, from_stop_id=None, to_stop_id=None)` supplies per-passage baselines. Road observations contain provider, directed geometry, duration, acquisition/expiry and response indices; they never contain a reusable bus coefficient.

- [ ] Write failing directed-overlap, reverse/parallel/ambiguous, partial interpolation, missing baseline, version mismatch, loop and TTL tests.
- [ ] Run focused offline tests and confirm failures identify absent behavior.
- [x] Implement compact edge data from the existing index; reject ambiguity and incompatible versions.
- [x] Implement bounded 1800-second cache with single-flight concurrent loading and no read renewal.
- [x] Verify focused tests, including concurrent errors, waiter deadline, repeated edge passages, and indexed selected-trip reads (no unfiltered stop_times scan).

### Task 2: Provider observations and bounded waypoint selection

**Files:** Create `backend/app/traffic_planner.py`; modify `backend/app/yandex_router.py`, `backend/app/dgis.py`.

**Interfaces:** Provider `estimate_observations(points, timeout_seconds=...)` supplies every alternative's timed step/path; adapters count each call once. Yandex accepts only traffic_type=realtime. 2GIS duration belongs to a complete outcoming_path, not each geometry item.

- [ ] Write failing fixtures for multiple alternatives, split geometry, terminal maneuver, malformed partial paths, non-realtime Yandex, quota/error and redaction.
- [ ] Verify failures; implement observations without changing existing road-only responses.
- [x] Select current-candidate edges before BFS layers 1 and 2; respect shared edges/line count/proximity and 50 points including repeats.
- [x] Run offline adapter tests; assert no second paid call for malformed chains or new tails.

### Task 3: Correction and temporal reconstruction

**Files:** Create `backend/app/traffic_engine.py`; modify `backend/app/otp.py` for necessary trip metadata and tail planning.

**Interfaces:** `TrafficEngine(planner, roads, dgis_roads, graph_loader=None, *, clock, wall_clock).plan(request, gtfs_version=None, graph_version=None) -> RoutesResponse`. Pure coverage and transfer helpers must agree with the public diagnostics.

- [x] Write failing tests for the approved 480-second/half-distance/360-second-car example: corrected movement=600, added=120.
- [x] Test source conflict selection, no negative corrections, original-baseline idempotence, dwell once, loop passage independence, partial applicability boundaries, mixed frequency exact_times windows, initial wait preservation and uncovered-tail preservation.
- [x] Apply disjoint fragment corrections, then reconstruct waits/transfers while retaining first departure.
- [x] Repair missed exact transfers and every delayed frequency transfer from corrected alighting stop/time, retaining prefix and request constraints/remaining transfer budget.
- [x] Stop identical no-progress recovery inputs; on deadline retain only reachable old frequency tails as unverified/incomplete, otherwise exclude and save original.
- [x] Run arithmetic, timeline and recovery tests offline.

### Task 4: API grouping, budgets and complete diagnostics

**Files:** Modify `backend/app/main.py`, schemas/OpenAPI descriptions as necessary. Create `backend/tests/test_traffic_integration.py`, `test_traffic_regressions.py`, and `fixtures/traffic/`.

**Interfaces:** Existing `/api/v1/routes` response; quality sufficient at >=0.8 of eligible distance; metro/rail/dedicated lanes excluded from denominator; null ratio for zero denominator.

- [x] Write integration tests for shared max_results, sufficient-before-incomplete, arrival/transfers/walk tie breaks and zero applicable distance.
- [x] Test provider budgets, remaining timeout for initial OTP and coalesced provider waits, timeline duration sums after recovery, repeated searches using app-scoped cache, dummy isolation and real errors without demo fallback.
- [x] Integrate engine and return trace/version/coverage/observation/source/recovery/provider-call diagnostics, including excluded original routes.
- [x] Run `cd backend && .venv/bin/python -m pytest -q`; expected all existing and new offline tests pass.
- [x] Regenerate OpenAPI, inspect changes, and report exact test results and unresolved accuracy limitations without making live paid calls.


## Verification checkpoint

Independent suite command (12 November 2026):

```sh
PYTHONPATH=backend backend/.venv/bin/python -m pytest backend/tests/test_traffic_integration.py backend/tests/test_traffic_regressions.py -q
```

Result: **70 passed**, 0.98 seconds test runtime. TestClient checks required execution outside the filesystem sandbox because its asyncio portal stalled inside the sandbox; HTTP provider calls remain mocked and all socket connections are prohibited by fixtures. No live paid call and no commit was made. The final integration gate subsequently passed; see the final results below.


Checkboxes mark observed implemented/tested work only. Remaining unchecked red/green history is not asserted retrospectively; the root reviewer completed the full-suite/OpenAPI final gate. Whole-edge provenance and partial lane-boundary applicability are covered by the graph owner tests.


## Final verification — 12 November 2026

- [x] Backend: **456 passed**, one warning, 9.58 seconds; **124 additional backend tests** over the verified 332-test baseline.
- [x] Other CI Python suites (data/GTFS, metro, rail, OTP): **103 passed**, 4.64 seconds.
- [x] Frontend: **14 passed** and `npm run build` succeeded.
- [x] Total automated tests: **573 passed**; `git diff --check` clean; generated OpenAPI matches the checked snapshot.
- [x] Fresh review regressions cover two successive tail recoveries for both exact and frequency service, single collection, global geometry order, timezone invariance, shape complexity and 50-point BFS-neighbor priority.

No live paid API calls or commits were made. Geometric thresholds are fixture-validated heuristics, not measured ETA accuracy. Curated lane evidence remains an optional external input; no official city-wide directed layer has been invented.
