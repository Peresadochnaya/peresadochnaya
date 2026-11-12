# GTFS-RT service

Independent service outside `backend/`. It publishes observed vehicle positions
and computes TripUpdates only when an observation identifies a static GTFS trip.

## Runtime

Traefik → `realtime:8001/api/v1/raw_feed` → last successful `feed.bin`.
The main API exposes the same contract in Swagger and proxies its fallback path.
`GET /api/v1/vehicles?dummy=false&bbox=29,59,31,61` goes through the main API to
this service. `dummy=true` retains the existing coherent demo.

One Uvicorn worker starts one asynchronous producer. A process-level `flock` on
the persistent feed directory prevents another producer from starting. Kubernetes
uses one replica and Recreate. Every iteration starts a Python worker process,
which downloads one observation feed and runs the C++ executable once for all
eligible vehicles. HTTP handlers remain available during generation. After the
iteration completes, the loop sleeps five seconds; runs never overlap.

The whole worker process group (including C++) has a 60-second timeout. The C++
call itself has a 45-second timeout. Shutdown cancels and kills that process group.
Nonzero exits, stale upstream feeds, malformed protobuf and timeouts leave the
previous file untouched. Only a fresh, valid FULL_DATASET candidate is fsynced and
atomically renamed over `feed.bin`. The 256 MiB PVC retains the last good file
across service replacement; no snapshot history is kept. `X-Feed-Timestamp` and
`X-Feed-Stale` describe its original source age, never a refreshed fake timestamp.
Before any successful generation, raw feed and real positions return 503.

Positions and TripUpdates come from the same protobuf snapshot. Coordinates,
heading (degrees clockwise from north), speed and observation timestamps come
from the source. Missing headings remain null. Bbox and modes filter before limit;
responses signal truncation and stale observations. `limit` defaults to 10000 and supports up to 20000;
a large bbox containing more vehicles returns `vehicles_truncated`.

Static lookup uses the immutable SQLite GTFS index corresponding to
`otp/current/manifest.json:gtfs_source_sha256`, not whichever static feed was
most recently downloaded. Index garbage collection preserves the loaded graph's
source index. No ZIP scans are performed by the generator. The existing TLS CA
bundle and Kubernetes `gtfs-proxy` secret are reused, with verification enabled.
Neither proxy credentials nor vehicle payloads are logged.

## MR24 review and fixes

MR24 was merged into `backa`, not deployed `main`. Only its internal Pydantic
TripUpdate models, builder and protobuf serializer were ported here. Its unused
Python wrapper assumed a nonexistent `pybind11.import_` and bindings which were
never defined. Its C++ matching always returned `nearest_stop_sequence=-1`.
The C++ input had no stop coordinates and interpreted array positions as GTFS
sequence numbers. There was no build, orchestration, HTTP service or test suite.

The service builds a standalone C++17 executable with warnings treated as errors.
A batch text protocol avoids a new pybind11 ABI/dependency. Geometry uses [lat,lon]
in the executable and actual stop_sequence values, including nonconsecutive ones.
Map matching projects onto the shape, interpolates scheduled travel time between
neighboring stops, and propagates the resulting delay to future stops. Ambiguous
backtracking/loops, observations over 150 m from shape and offsets over two hours
are excluded from TripUpdates. This is a simple model, not a calibrated traffic
prediction; stop dwell and delay recovery are not modeled. Source coordinates
remain visible even when delay calculation is rejected.

Epoch seconds use 64-bit integers. GTFS times beyond 24:00 retain their service
date. Frequency trips require observed start_date and start_time; exact_times=0
predictions use absolute event times and UNSCHEDULED instead of schedule-relative
delays. There is no invented vehicle→trip assignment.

### Actual upstream limitation

A live portal snapshot inspected on 2026-11-12 contained 2674 VehiclePositions,
2334 fresh enough at inspection, all with bearing. **None had trip_id, start_time,
start_date or current_stop_sequence**, only route_id. Route IDs identify lines,
not a particular scheduled trip/direction. This source therefore produces real
map positions but zero safe TripUpdates with the current matching contract.

OTP's stop-time-updater polls the feed every 10 seconds for surface feed **2**
(verified against the running graph: feed 1 metro, 2 surface, 3 rail). It can consume
TripUpdates when trip-bound observations become available. Vehicle coordinates
alone do not modify OTP arrival times. A route/geometry/time-based probabilistic
vehicle→trip association would be a separate algorithm with ambiguity/confidence
handling; silently choosing one would create incorrect delays.

References:
- https://docs.opentripplanner.org/en/dev-2.x/GTFS-RT-Config/
- https://gtfs.org/documentation/realtime/reference/

## Local build and tests

From repository root:

```sh
pip install -r realtime/requirements.txt pytest
mkdir -p .bench-data
g++ -std=c++17 -O2 -Wall -Wextra -Werror realtime/cpp/delay_engine.cpp -o .bench-data/delay-engine
DELAY_ENGINE_BINARY="$PWD/.bench-data/delay-engine" PYTHONPATH=realtime pytest realtime/tests
```

Container: `docker build -f realtime/Dockerfile .`. CI tests C++ and Python and
builds amd64/arm64 image `peresadochnaya:realtime-<commit>` (the existing public GHCR package) separately from API.
Config: DATA_ROOT, REALTIME_DIR, GTFS_RT_INTERVAL_SECONDS, GTFS_RT_TIMEOUT_SECONDS,
GTFS_RT_SOURCE_URL, GTFS_RT_CA_FILE, DELAY_ENGINE_BINARY and HTTPS_PROXY.
