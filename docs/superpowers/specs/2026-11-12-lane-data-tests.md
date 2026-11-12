# Directed bus-lane acceptance

This matrix extends decisions D11, D18 and D34. Tests are offline; no traffic-provider quota is consumed. The evidence layer may exclude only the measured directed portion of a transit edge. Unmapped or uncertain streets stay unknown and remain in the applicable-distance denominator.

| Contract | Independent evidence |
| --- | --- |
| Partial lane exclusion | `test_partial_lane_preserves_full_edge_time_density_and_unknown_remainder`: half a 480-second edge is excluded; a 960-second full-edge road observation adds exactly 240 seconds on the other half. |
| Directed geometry | Reverse travel and perpendicular crossing produce zero corrected coverage. |
| Invalid intervals | Overlap, outside-edge endpoints and NaN cannot reduce applicable distance. |
| Atomic replacement | A valid first row followed by an invalid row leaves the previously loaded version and records intact. |
| Real evidence | Production fixture is nonempty, every span traces to archived OSM or independently resolved official geometry with source-specific provenance, and all summaries reconcile. |
| Identity and overrides | Feed mismatch rejects; directed geometry mismatch stays unknown; explicit runtime override wins and malformed override leaves search usable. |
| Conditional and direction semantics | Time restrictions and opening hours remain unknown; side and counterflow tags retain OSM direction. |
| Official completeness | All 34 inventory rows and 3 supplements are explicitly accounted: one resolved official corridor, 36 unresolved; no name-only match claims completeness. |

Geometry agreement is not a claim of legal lane availability or measured travel-time accuracy. Time-limited rules require explicit supported applicability; a guessed default cannot become unconditional dedicated evidence. Pavement, tram tracks, proximity and opposite carriageways do not independently establish a dedicated bus lane.

Independent run: 35 passing cases using `cd backend && .venv/bin/python -m pytest tests/test_traffic_lane_acceptance.py -q`. The real layer combines explicit historical OSM tags with one independently resolved official corridor. Official-corridor endpoint truncation is verified against the real Malaya Morskaya source: a line extended past both intersections gains coverage only inside official boundaries; 1 of 37 official inventory/supplement items has verified full geometric resolution: Malaya Morskaya, six reversed main-tertiary ways, 499.680 metres. The other 36 remain unresolved. `official-coverage.json` records each unresolved item explicitly. This is a remaining coverage limitation, not evidence of absence of lanes.

Geometry-variant acceptance additionally selects only the matching directed fingerprint and rejects duplicate fingerprints, negative intervals and invalid timestamps before atomic installation. The real-fixture audit sums all variants; these metres count directed GTFS variants and must not be described as physical city-wide lane length.

Cache-order independence is verified through the actual ObservationCollector BFS: loading an incompatible neighboring trip cannot change the selected exact trip correction; the auxiliary representative remains ambiguous. A separate offline actual-index proof loads 3 to 14 trips while all 31 selected occurrence measurements remain exactly equal. Strict recorded-OTP replay refuses responses when outgoing input differs and reports recorded_tail_input_mismatch instead of claiming live planner failure.

## Final verification checkpoint

After the lane-data, exact recovery-duplicate and cache-order fixes, the full backend suite passed **509 tests in 10.60 seconds**, with one warning: **177 above the original 332-test baseline**. The OpenAPI snapshot matches and `git diff --check` is clean. Previously verified unchanged components contribute 103 other CI Python tests and 14 frontend tests, for **626 automated tests total**; the frontend build succeeded. The 35 independent lane-acceptance cases, 8 builder cases and 41-case acceptance/recovery subset are overlapping views within that backend total, not additional tests to sum.
