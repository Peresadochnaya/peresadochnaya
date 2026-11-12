"""Six read-only GTFS endpoints, deliberately independent of the route planner."""
import math
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from .gtfs_schemas import (GtfsDeparturesResponse, GtfsDirection, GtfsRouteResponse,
    GtfsRoutesResponse, GtfsStopResponse, GtfsStopsResponse, GtfsTripResponse)
from .gtfs_store import GtfsError, GtfsStore
from .schemas import ErrorResponse, ValidationErrorResponse

MODES = {'bus','tram','trolleybus','metro','rail','unknown'}


def parse_modes(value):
    if value is None: return None
    modes = value.split(',')
    if not modes or any(m not in MODES for m in modes):
        raise GtfsError(422,'invalid_modes','Modes must be a comma-separated list of supported transit modes')
    return set(modes)


def parse_bbox(value):
    try:
        parts = value.split(',')
        if len(parts) != 4: raise ValueError()
        a,b,c,d=map(float,parts)
        if not all(math.isfinite(x) for x in (a,b,c,d)): raise ValueError()
        if not (-180 <= a < c <= 180 and -90 <= b < d <= 90): raise ValueError()
        return a,b,c,d
    except ValueError:
        raise GtfsError(422,'invalid_bbox','bbox must be minLon,minLat,maxLon,maxLat with valid increasing WGS84 bounds') from None


def create_gtfs_router(root: Path) -> APIRouter:
    router = APIRouter(prefix='/api/v1',tags=['GTFS'])
    store = GtfsStore(root)
    errors={404:{'model':ErrorResponse},422:{'model':ErrorResponse|ValidationErrorResponse},503:{'model':ErrorResponse}}

    def execute(dummy, action):
        try:
            with store.open(dummy=dummy) as snapshot: return action(snapshot)
        except GtfsError as e:
            return JSONResponse(status_code=e.status,content={'error':{'code':e.code,'message':e.message}})

    @router.get('/stops',response_model=GtfsStopsResponse,responses=errors)
    def stops(bbox: str=Query(),q: str|None=Query(default=None,min_length=1,max_length=256,pattern=r'\S'),
              modes: str|None=Query(default=None),limit:int=Query(default=500,ge=1,le=2000),
              offset:int=Query(default=0,ge=0),dummy:bool=Query(default=True, description="true: согласованные демо-данные без внешних запросов; false: реальные источники")):
        def action(s):
            bounds=parse_bbox(bbox);wanted=parse_modes(modes)
            serving=s.stop_routes() if wanted is not None else None
            rows=[]
            for stop_id,stop in sorted(s.stops.items()):
                if not (bounds[0] <= stop.lon <= bounds[2] and bounds[1] <= stop.lat <= bounds[3]):continue
                if q and q.strip().casefold() not in stop.name.casefold() and q.strip().casefold() not in (stop.code or '').casefold():continue
                if wanted is not None and not any(s.routes[r].mode in wanted for r in serving.get(stop_id,())):continue
                rows.append(stop)
            return GtfsStopsResponse(**s.metadata,items=rows[offset:offset+limit],offset=offset,limit=limit,total=len(rows),has_more=offset+limit<len(rows))
        return execute(dummy,action)

    @router.get('/stops/{stop_id}',response_model=GtfsStopResponse,responses=errors)
    def stop(stop_id:str,dummy:bool=Query(default=True, description="true: согласованные демо-данные без внешних запросов; false: реальные источники")):
        def action(s):
            obj=s.get_stop(stop_id)
            lines=[s.routes[r] for r in sorted(s.stop_routes().get(stop_id,()))]
            return GtfsStopResponse(**s.metadata,stop=obj,routes=lines)
        return execute(dummy,action)

    @router.get('/stops/{stop_id}/departures',response_model=GtfsDeparturesResponse,responses=errors)
    def departures(stop_id:str,departure_at:AwareDatetime,lookahead_minutes:int=Query(default=120,ge=1,le=1440),
                   limit:int=Query(default=20,ge=1,le=100),dummy:bool=Query(default=True, description="true: согласованные демо-данные без внешних запросов; false: реальные источники")):
        def action(s):
            items,more,warnings=s.departures(stop_id,departure_at,lookahead_minutes,limit)
            meta=s.metadata;meta['warnings'].extend(warnings)
            return GtfsDeparturesResponse(**meta,stop_id=stop_id,departure_at=departure_at,
                lookahead_minutes=lookahead_minutes,limit=limit,items=items,has_more=more)
        return execute(dummy,action)

    @router.get('/transit-routes',response_model=GtfsRoutesResponse,responses=errors)
    def lines(q:str|None=Query(default=None,min_length=1,max_length=256,pattern=r'\S'),
              modes:str|None=Query(default=None),limit:int=Query(default=100,ge=1,le=500),
              offset:int=Query(default=0,ge=0),dummy:bool=Query(default=True, description="true: согласованные демо-данные без внешних запросов; false: реальные источники")):
        def action(s):
            wanted=parse_modes(modes)
            rows=[v for _,v in sorted(s.routes.items()) if (wanted is None or v.mode in wanted)
                  and (not q or q.strip().casefold() in (v.short_name+' '+v.long_name).casefold())]
            return GtfsRoutesResponse(**s.metadata,items=rows[offset:offset+limit],offset=offset,limit=limit,total=len(rows),has_more=offset+limit<len(rows))
        return execute(dummy,action)

    @router.get('/transit-routes/{route_id}',response_model=GtfsRouteResponse,responses=errors)
    def line(route_id:str,limit:int=Query(default=50,ge=1,le=200),offset:int=Query(default=0,ge=0),dummy:bool=Query(default=True, description="true: согласованные демо-данные без внешних запросов; false: реальные источники")):
        def action(s):
            route=s.get_route(route_id)
            rows=[t for _,t in sorted(s.trips.items()) if t.route_id==route_id]
            directions=sorted({(t.direction_id,t.headsign) for t in rows},key=lambda t:(-1 if t[0] is None else t[0],t[1] or ''))
            return GtfsRouteResponse(**s.metadata,line=route,directions=[GtfsDirection(direction_id=a,headsign=b) for a,b in directions],
                trips=rows[offset:offset+limit],offset=offset,limit=limit,total=len(rows),has_more=offset+limit<len(rows))
        return execute(dummy,action)

    @router.get('/trips/{trip_id}',response_model=GtfsTripResponse,responses=errors)
    def trip(trip_id:str,dummy:bool=Query(default=True, description="true: согласованные демо-данные без внешних запросов; false: реальные источники")):
        def action(s):
            result,warnings=s.trip(trip_id);meta=s.metadata;meta['warnings'].extend(warnings)
            return GtfsTripResponse(**meta,trip=result)
        return execute(dummy,action)

    return router
