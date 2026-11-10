"""Replay saved live samples only for matching inputs, never as fresh results."""
import json
from functools import lru_cache
from pathlib import Path

from .schemas import GeocodeResponse, RoadRouteRequest, RoadRouteResponse, SuggestResponse


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "yandex" / "live-2026-11-11.json"
DUMMY_URI_PREFIX = "dummy:yandex:2026-11-11:suggest:"


@lru_cache(maxsize=1)
def recorded_samples() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def replay_warning() -> str:
    return (f"Recorded Yandex fixture from {recorded_samples()['captured_at']}; "
            "replay only, not a current lookup; Yandex was not called")


def recorded_suggest(q: str, limit: int) -> SuggestResponse | None:
    case = recorded_samples()["cases"]["suggest"]
    if q.strip().casefold() != case["request"]["params"]["q"].casefold():
        return None
    response = SuggestResponse.model_validate(case["api_response"])
    response.mode = "dummy"
    response.items = response.items[:limit]
    for index, item in enumerate(response.items):
        item.uri = f"{DUMMY_URI_PREFIX}{index}"
    response.warnings = [replay_warning(), "Dummy URIs must not be sent to Yandex"]
    return response


def recorded_geocode(q: str | None, uri: str | None) -> GeocodeResponse | None:
    cases = recorded_samples()["cases"]
    case = cases["geocode"]
    captured_uri = case["request"]["params"].get("uri")
    labels = {item["label"].casefold() for item in case["api_response"]["items"]}
    # Only the first suggestion was geocoded live. Do not claim that its
    # coordinates resolve the other two suggestions from a different city.
    labels.add(cases["suggest"]["api_response"]["items"][0]["title"].casefold())
    matches = uri is not None and uri in (captured_uri, f"{DUMMY_URI_PREFIX}0")
    if not matches and not (q is not None and q.strip().casefold() in labels):
        return None
    response = GeocodeResponse.model_validate(case["api_response"])
    response.mode = "dummy"
    response.warnings = [replay_warning()]
    return response


def recorded_reverse(lat: float, lon: float) -> GeocodeResponse | None:
    case = recorded_samples()["cases"]["reverse"]
    params = case["request"]["params"]
    if (lat, lon) != (params["lat"], params["lon"]):
        return None
    response = GeocodeResponse.model_validate(case["api_response"])
    response.mode = "dummy"
    response.warnings = [replay_warning()]
    return response


def recorded_road_route(request: RoadRouteRequest) -> RoadRouteResponse | None:
    case = recorded_samples()["cases"]["road-route"]
    if request.model_dump(mode="json", exclude_none=True) != case["request"]["body"]:
        return None
    response = RoadRouteResponse.model_validate(case["api_response"])
    response.mode = "dummy"
    # Keep the captured calculation timestamp, but do not advertise its traffic
    # information as realtime in a dummy response.
    response.traffic_type = None
    response.warnings = [replay_warning(), *response.warnings]
    return response
