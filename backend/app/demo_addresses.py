"""Provider-shaped dummy addresses from the same small GTFS demo as vehicles."""
from .schemas import GeocodedPlace, GeocodeResponse, Suggestion, SuggestResponse


def _stops():
    from .gtfs_store import demo_stops
    return demo_stops()


def suggestions(provider: str, q: str, limit: int) -> SuggestResponse:
    stops = _stops()
    matches = [s for s in stops if q.strip().casefold() in s['name'].casefold()]
    selected = (matches or stops)[:limit]
    return SuggestResponse(mode="dummy", provider=provider, items=[Suggestion(
        title=s['name'], subtitle="Демо: остановка из GTFS Санкт-Петербурга",
        uri=f"dummy:{provider}:stop:{s['stop_id']}") for s in selected],
        warnings=["Small GTFS demo dataset only; not a real address search; no external API was called"])


def geocode(provider: str, q: str | None, uri: str | None) -> GeocodeResponse:
    stops = _stops()
    prefix = f"dummy:{provider}:stop:"
    if uri is not None and uri.startswith(prefix):
        stop = next((s for s in stops if s['stop_id'] == uri[len(prefix):]), None)
    elif q is not None:
        stop = next((s for s in stops if q.strip().casefold() in s['name'].casefold()), None)
    else:
        stop = None
    # A free text demo may fall back to the first sample; never assign one
    # suggestion's coordinates to an unknown provider-specific identifier.
    if stop is None and uri is None and stops:
        stop = stops[0]
    return GeocodeResponse(mode="dummy", provider=provider,
        items=[GeocodedPlace(lat=stop['lat'], lon=stop['lon'], label=stop['name'])] if stop else [],
        warnings=["GTFS demo stop coordinates, not a real geocoder result; no external API was called"])


def reverse(provider: str, lat: float, lon: float) -> GeocodeResponse:
    stops = _stops()
    stop = min(stops, key=lambda s: (s['lat']-lat)**2 + (s['lon']-lon)**2) if stops else None
    return GeocodeResponse(mode="dummy", provider=provider,
        items=[GeocodedPlace(lat=stop['lat'], lon=stop['lon'], label=stop['name'])] if stop else [],
        warnings=["Nearest stop in a small GTFS demo subset, not a street address; keep the clicked routing coordinates; no external API was called"])
