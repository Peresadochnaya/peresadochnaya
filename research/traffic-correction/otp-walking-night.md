# Long walking requests and empty overnight results

Verified 2026-11-13 against the running OTP GraphQL schema, with read-only
introspection. No paid road-provider calls were made.
Raw type introspection is saved in `otp-walking-live-schema.json`.

## What can be configured

`WalkPreferencesInput` contains exactly `boardCost`, `reluctance`,
`safetyFactor`, and `speed`. `PlanPreferencesInput` contains `accessibility`,
`street`, and `transit`. `planConnection` has no direct-street duration argument.
Consequently neither `preferences.street.walk.maxDirectDuration` nor a generic
`direct.maxDuration` is supported by this deployment's GTFS GraphQL API.

Realtime exclusion is nested under
`preferences.transit.timetable.excludeRealTimeUpdates: true`.
`preferences.timetable` is not a valid field. `PlanModesInput` supports both
`direct: [WALK]` and `directOnly: true` for a walking-only street search.

The local `routingDefaults.accessEgress.maxDuration: PT30M` limits access and
egress searches; it is not the direct origin-to-destination walking limit.
Do not raise it to fix long walking requests.

Official OTP development source defines the default direct-street duration as
four hours and accepts router configuration
`routingDefaults.maxDirectStreetDurationForMode: {"WALK": "PT8H"}` to override
walking specifically. `maxDirectStreetDuration` changes the generic default.
These are performance limits: results near the limit need not be optimal.
An eight-hour walking limit is a possible bounded city-scale policy, not a
per-request GraphQL preference. It requires an OTP configuration rollout.
The deployed build's exact default was not read from the running Java object;
the source branch may differ from the deployed snapshot.

Sources:

- [StreetPreferences.java](https://github.com/opentripplanner/OpenTripPlanner/blob/dev-2.x/application/src/main/java/org/opentripplanner/routing/api/request/preference/StreetPreferences.java)
- [RouteRequestConfig.java](https://github.com/opentripplanner/OpenTripPlanner/blob/dev-2.x/application/src/main/java/org/opentripplanner/standalone/config/routerequest/RouteRequestConfig.java)

## Empty results

An empty OTP `edges` list is a valid empty result; it must remain `routes: []`.
Preserve OTP routing errors as warnings and explain empty results in the UI.
The absence of public transport at night does not establish that a walking
street path exists, and increasing a search limit does not create one.
Never substitute straight-line geometry for a missing OTP itinerary.

The independent offline tests in `backend/tests/test_walking_night.py` exercise
a three-hour walking protocol fixture, preserving OTP geometry and timings,
and an empty result with an OTP error. The fixture is not live route evidence
and does not measure OTP's street-search duration limit.

## Live overnight walking probes

Two own-OTP requests used departure `2026-11-13T03:00:00+03:00`,
`modes: {direct: [WALK], directOnly: true}` and walking speed 1.3 m/s.
No traffic or geocoding provider was used. Raw requests and responses are in
`night-walk-live/`.

| Request coordinates (lat, lon) | Observed result |
| --- | --- |
| Pionerskaya `(60.0029,30.2963)` → Palace `(59.939,30.315)` | WALK, 8444.22 m, 7049 s; 03:00–04:57:29; no routing errors |
| Pionerskaya `(60.0029,30.2963)` → Pulkovo `(59.7999,30.273)` | HTTP 200, no itineraries; `NO_DIRECT_MODE_CONNECTION` |

The second error explicitly allows either excessive distance or lack of a
connected street path. It does not identify the duration limit as the cause.
These are reference cases, not the user's as-yet-unspecified original request.
