"""Read the separate realtime service; HTTP requests never run a generator."""
import os
import httpx
from fastapi import APIRouter
from fastapi.responses import Response, JSONResponse
from .schemas import ErrorResponse


def request(path, params=None):
    url=os.getenv('REALTIME_URL','')
    if not url: raise httpx.ConnectError('Realtime service not configured')
    response=httpx.get(url.rstrip('/')+path,params=params,timeout=5,trust_env=False)
    response.raise_for_status()
    return response


def create_realtime_router():
    router=APIRouter(tags=['GTFS-RT'])
    @router.get('/api/v1/raw_feed',response_class=Response,
        description='Last successful GTFS-RT FULL_DATASET protobuf with computed TripUpdates and observed VehiclePositions. No generation is triggered. TripUpdates are emitted only for safely identified trips; route_id-only observations remain VehiclePositions and do not change OTP travel times. Preserved after generator errors; inspect X-Feed-Timestamp and X-Feed-Stale. 503 before the first successful generation.',
        responses={200:{'content':{'application/octet-stream':{'schema':{'type':'string','format':'binary'}}},
            'headers':{'X-Feed-Timestamp':{'description':'Original upstream snapshot Unix timestamp','schema':{'type':'integer'}},
                       'X-Feed-Stale':{'description':'true when the preserved snapshot is older than 90 seconds or over 30 seconds in the future','schema':{'type':'string','enum':['true','false']}}}},503:{'model':ErrorResponse}})
    def raw_feed():
        try:
            result=request('/api/v1/raw_feed')
            return Response(result.content,media_type='application/octet-stream',
                headers={key:result.headers[key] for key in ('cache-control','x-feed-timestamp','x-feed-stale') if key in result.headers})
        except httpx.HTTPError:
            return JSONResponse(status_code=503,content={'error':{'code':'realtime_unavailable','message':'GTFS-RT feed is not available'}})
    return router
