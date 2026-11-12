"""2GIS address and driving adapters. A public operation makes one request.

Documentation:
https://docs.2gis.com/en/api/search/suggest/reference/3.0/suggests
https://docs.2gis.com/en/api/search/geocoder/reference/3.0/items/geocode
https://docs.2gis.com/en/api/search/places/reference/3.0/items/byid
https://docs.2gis.com/en/api/navigation/routing/reference/routing
"""
import math
import os
import re
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError

from .schemas import GeocodeResponse, GeocodedPlace, RoadRoute, RoadRouteRequest, RoadRouteResponse, Suggestion, SuggestResponse
from .yandex import ProviderError


CATALOG_URL = "https://catalog.api.2gis.com/3.0"
SUGGEST_URL = CATALOG_URL + "/suggests"
GEOCODER_URL = CATALOG_URL + "/items/geocode"
BY_ID_URL = CATALOG_URL + "/items/byid"
ROUTER_URL = "https://routing.api.2gis.com/routing/7.0.0/global"
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,512}\Z")
DRIVING_WARNING = "Driving estimate only; not a bus duration or a confirmed public transport itinerary"
VIEWPORT = {"viewpoint1": "26.5,61.4", "viewpoint2": "35.8,58.3"}


class DgisClient:
    error_prefix = "dgis_address_provider"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client

    def fail(self, status: int, suffix: str, message: str):
        raise ProviderError(status, f"{self.error_prefix}_{suffix}", message) from None

    def _request(self, method: str, url: str, *, params: dict | None = None,
                 body: dict | None = None, catalog: bool = False, timeout_seconds: float = 8) -> dict:
        key = os.getenv("DGIS_API_KEY", "").strip()
        if not key:
            self.fail(503, "unavailable", "2GIS provider key is not configured")
        try:
            kwargs = {"params": {**(params or {}), "key": key}, "timeout": timeout_seconds, "follow_redirects": False}
            if body is not None:
                kwargs["json"] = body
            if self.client is not None:
                response = self.client.request(method, url, **kwargs)
            else:
                with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout_seconds) as client:
                    response = client.request(method, url, **kwargs)
            if response.status_code == 429:
                self.fail(503, "quota", "2GIS quota or rate limit reached")
            if response.status_code in (408, 504):
                self.fail(504, "timeout", "2GIS provider timed out")
            if response.status_code != 200:
                self.fail(502, "error", "2GIS provider rejected the request")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected response object")
            if catalog:
                # Catalog API can return logical errors inside HTTP 200.
                code = data["meta"]["code"]
                if type(code) is not int:
                    raise ValueError("Invalid meta code")
                if code == 404:
                    return {"meta": {"code": 404}, "result": {"items": []}}
                if code == 429:
                    self.fail(503, "quota", "2GIS quota or rate limit reached")
                if code in (408, 504):
                    self.fail(504, "timeout", "2GIS provider timed out")
                if code != 200:
                    self.fail(502, "error", "2GIS provider rejected the request")
            return data
        except httpx.TimeoutException:
            self.fail(504, "timeout", "2GIS provider timed out")
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            self.fail(502, "error", "Invalid response from 2GIS provider")

    def _items(self, data: dict) -> list:
        try:
            items = data["result"]["items"]
            if not isinstance(items, list):
                raise ValueError("Expected items list")
            return items
        except (KeyError, TypeError, ValueError):
            self.fail(502, "error", "Invalid results from 2GIS provider")


def _nonempty_text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected non-empty text")
    return value


def _object_id(value) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise ValueError("Invalid object ID")
    return value


class DgisAddresses(DgisClient):
    def suggest(self, q: str, limit: int) -> SuggestResponse:
        data = self._request("GET", SUGGEST_URL, catalog=True, params={
            "q": q, "page_size": limit, "locale": "ru_RU", "suggest_type": "route_endpoint",
            "type": "building,street,station,adm_div.city,adm_div.settlement,adm_div.district",
            **VIEWPORT,
        })
        try:
            items = []
            for row in self._items(data)[:limit]:
                name = _nonempty_text(row["name"])
                subtitle = row.get("full_name") or row.get("address_name")
                if subtitle is not None:
                    subtitle = _nonempty_text(subtitle)
                items.append(Suggestion(title=name, subtitle=subtitle if subtitle != name else None,
                                        uri="dgis:" + _object_id(row["id"])))
            return SuggestResponse(mode="2gis", provider="2gis", items=items)
        except (KeyError, TypeError, AttributeError, ValueError, ValidationError):
            self.fail(502, "error", "Invalid suggestions from 2GIS provider")

    def geocode(self, *, q: str | None = None, uri: str | None = None,
                lat: float | None = None, lon: float | None = None) -> GeocodeResponse:
        choices = int(q is not None) + int(uri is not None) + int(lat is not None or lon is not None)
        if choices != 1 or (lat is None) != (lon is None):
            raise ProviderError(422, "invalid_geocode_query", "Provide an address, a 2GIS URI or both coordinates")
        params = {"locale": "ru_RU", "fields": "items.point,items.full_address_name"}
        url = GEOCODER_URL
        if uri is not None:
            try:
                if not uri.startswith("dgis:"):
                    raise ValueError("Wrong URI provider")
                object_id = _object_id(uri[5:])
            except ValueError:
                raise ProviderError(422, "invalid_geocode_uri", "Provide a 2GIS suggestion URI; dummy and foreign URIs are not accepted") from None
            # By-ID resolution is one Places API request. The key must grant
            # this product; never fall back to an additional geocoder call.
            params["id"] = object_id
            url = BY_ID_URL
        elif lat is not None:
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                raise ProviderError(422, "invalid_coordinates", "Coordinates are outside WGS84")
            params.update(lat=lat, lon=lon, page_size=1)
        else:
            params.update(q=q, page_size=1, **VIEWPORT)
        data = self._request("GET", url, params=params, catalog=True)
        try:
            items = []
            for row in self._items(data)[:1]:
                point = row["point"]
                if any(type(point[k]) not in (int, float) or not math.isfinite(point[k]) for k in ("lat", "lon")):
                    raise ValueError("Invalid coordinate")
                label = row.get("full_address_name") or row.get("full_name") or row.get("address_name") or row.get("name")
                # Do not invent Yandex-style precision for a 2GIS result.
                items.append(GeocodedPlace(lat=point["lat"], lon=point["lon"], label=_nonempty_text(label), precision=None))
            return GeocodeResponse(mode="2gis", provider="2gis", items=items)
        except (KeyError, TypeError, AttributeError, ValueError, ValidationError):
            self.fail(502, "error", "Invalid coordinates from 2GIS provider")


def driving_body(request: RoadRouteRequest, now: datetime) -> dict:
    body = {"points": [{"type": "stop", "lon": point.lon, "lat": point.lat} for point in request.points],
            "transport": "driving", "output": "detailed", "locale": "ru", "alternative": 0}
    if request.traffic_mode == "off":
        # Documented shortest-by-distance routing ignores jams; its geometry
        # can differ from fastest routing. Do not call it a same-path baseline.
        body["route_mode"] = "shortest"
    else:
        body.update(route_mode="fastest", traffic_mode="jam")
        if request.departure_at is not None:
            if request.departure_at < now:
                raise ProviderError(422, "invalid_departure_time", "Traffic forecast departure must not be in the past")
            body.update(traffic_mode="statistics", utc=math.ceil(request.departure_at.timestamp()))
    return body


def _wkt_line(value: str) -> list[tuple[float, float]]:
    if not isinstance(value, str):
        raise ValueError("Expected WKT")
    match = re.fullmatch(r"\s*LINESTRING(?:\s+Z)?\s*\(([^()]+)\)\s*", value, re.I)
    if match is None:
        raise ValueError("Expected LineString WKT")
    points = []
    for item in match.group(1).split(","):
        values = item.split()
        if len(values) not in (2, 3):
            raise ValueError("Invalid WKT position")
        position = [float(v) for v in values]
        if not all(math.isfinite(v) for v in position):
            raise ValueError("Invalid WKT number")
        lon, lat = position[:2]
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("WKT outside WGS84")
        points.append((lon, lat))
    if len(points) < 2:
        raise ValueError("Invalid WKT line")
    return points


def parse_road_route(data: dict, request: RoadRouteRequest, now: datetime) -> RoadRouteResponse:
    try:
        status = data["status"]
        if data["type"] not in ("result", "error"):
            raise ValueError("Invalid response type")
        common = {"mode": "2gis", "provider": "2gis", "traffic_mode": request.traffic_mode,
                  "traffic_type": "disabled" if request.traffic_mode == "off" else ("forecast" if request.departure_at else "realtime"),
                  "departure_at": request.departure_at if request.traffic_mode == "on" else None,
                  "calculated_at": now}
        warnings = [DRIVING_WARNING, "Shortest (traffic off) and fastest (traffic on) may use different road trajectories"]
        if status in ("ROUTE_NOT_FOUND", "ROUTE_DOES_NOT_EXISTS", "ATTRACT_FAIL", "POINT_EXCLUDED"):
            return RoadRouteResponse(**common, route=None, warnings=warnings + ["2GIS could not build the requested road route"])
        if status != "OK" or data["type"] != "result":
            raise ValueError("Route calculation failed")
        results = data["result"]
        if not isinstance(results, list) or not results:
            raise ValueError("Missing route result")
        item = results[0]
        for field in ("total_distance", "total_duration"):
            value = item[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid route measurement")
        if item.get("type", "carrouting") != "carrouting":
            raise ValueError("Expected driving result")
        maneuvers = item["maneuvers"]
        if not isinstance(maneuvers, list) or not maneuvers:
            raise ValueError("Missing maneuvers")
        points = []
        for maneuver in maneuvers:
            path = maneuver.get("outcoming_path")
            if path is None:
                continue  # Final maneuver has no outgoing road section.
            geometry = path["geometry"]
            if not isinstance(geometry, list) or not geometry:
                raise ValueError("Missing path geometry")
            for part in geometry:
                line = _wkt_line(part["selection"])
                if points and points[-1] != line[0]:
                    raise ValueError("Disconnected road geometry")
                points.extend(line[1:] if points else line)
        route = RoadRoute(distance_meters=item["total_distance"], duration_seconds=item["total_duration"],
                          geometry={"type": "LineString", "coordinates": points})
        if request.departure_at is not None and request.traffic_mode == "on":
            warnings.append("2GIS future departure uses statistical traffic, not a realtime observation")
        return RoadRouteResponse(**common, route=route, warnings=warnings)
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError, ValidationError):
        raise ProviderError(502, "dgis_router_provider_error", "Invalid route response from 2GIS provider") from None


class DgisRouter(DgisClient):
    error_prefix = "dgis_router_provider"

    def estimate_observations(self,points,timeout_seconds=8):
        if timeout_seconds<=0: raise ProviderError(504,'dgis_router_provider_timeout','Search deadline reached')
        if len(points)<2: raise ValueError('2GIS requires at least two points')
        coordinates=[(p.lon,p.lat) if hasattr(p,'lon') else p for p in points]
        body={'points':[{'type':'stop','lon':lon,'lat':lat} for lon,lat in coordinates],'transport':'driving','route_mode':'fastest','traffic_mode':'jam','output':'detailed','locale':'ru','alternative':3}
        data=self._request('POST',ROUTER_URL,body=body,timeout_seconds=min(8,timeout_seconds))
        return parse_observations(data,datetime.now(timezone.utc))

    def estimate(self, request: RoadRouteRequest) -> RoadRouteResponse:
        body = driving_body(request, datetime.now(timezone.utc))
        data = self._request("POST", ROUTER_URL, body=body)
        return parse_road_route(data, request, datetime.now(timezone.utc))


def parse_observations(data,received_at):
    """Retain every alternative and whole outcoming_path times exactly once."""
    from .traffic_data import RoadObservation
    stamp=received_at.timestamp() if isinstance(received_at,datetime) else float(received_at)
    try:
        if data['status'] in ('ROUTE_NOT_FOUND','ROUTE_DOES_NOT_EXISTS','ATTRACT_FAIL','POINT_EXCLUDED'): return []
        if data['status']!='OK' or data['type']!='result': raise ValueError('Failed result')
        observations=[]
        for alternative,item in enumerate(data['result']):
            if item.get('type','carrouting')!='carrouting': raise ValueError('Expected driving result')
            for index,maneuver in enumerate(item['maneuvers']):
                path=maneuver.get('outcoming_path')
                if path is None: continue
                line=[]
                for part in path['geometry']:
                    points=_wkt_line(part['selection'])
                    if line and line[-1]!=points[0]: raise ValueError('Disconnected geometry')
                    line.extend(points[1:] if line else points)
                observations.append(RoadObservation(f'2gis:{stamp}:{alternative}:{index}','2gis',tuple(line),path['distance'],path['duration'],stamp,alternative_index=alternative,part_index=index))
        return observations
    except (KeyError,TypeError,AttributeError,ValueError,IndexError,OverflowError):
        raise ProviderError(502,'dgis_router_provider_error','Invalid realtime observation from 2GIS provider') from None
