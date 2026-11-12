"""A single producer, asynchronous HTTP, last successful atomic GTFS-RT feed."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import signal
import sys
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, JSONResponse
from google.transit import gtfs_realtime_pb2 as pb

LOG=logging.getLogger('realtime')


def read_feed(path):
    payload=path.read_bytes()
    if len(payload)>20*1024*1024: raise ValueError('Feed too large')
    feed=pb.FeedMessage.FromString(payload)
    if not feed.IsInitialized() or feed.header.gtfs_realtime_version != '2.0' or not feed.header.HasField('timestamp') or feed.header.incrementality!=pb.FeedHeader.FULL_DATASET:
        raise ValueError('Invalid generated feed')
    return payload,feed


class Producer:
    def __init__(self, directory, *, interval=5, timeout=60, command=None):
        self.directory=Path(directory)
        self.interval=interval; self.timeout=timeout
        self.command=command or [sys.executable,'-m','realtime.worker']
        self.current=self.directory/'feed.bin'
        self.lock=None

    async def generate_once(self):
        candidate=self.directory/'candidate.bin'
        candidate.unlink(missing_ok=True)
        candidate.with_suffix('.json').unlink(missing_ok=True)
        process=await asyncio.create_subprocess_exec(*self.command,'--output',str(candidate),start_new_session=True,
                                                     stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(),self.timeout)
            if process.returncode: raise RuntimeError(f'Generator exit {process.returncode}')
            _,feed=await asyncio.to_thread(read_feed,candidate)
            if not -30<=time.time()-feed.header.timestamp<=90: raise ValueError('Candidate feed is stale')
            with candidate.open('rb') as stream: os.fsync(stream.fileno())
            os.replace(candidate,self.current)
            if candidate.with_suffix('.json').exists():
                os.replace(candidate.with_suffix('.json'),self.directory/'stats.json')
            LOG.info('Published GTFS-RT: %s entities',len(feed.entity))
        finally:
            if process.returncode is None:
                # Kill the whole worker+C++ process group on timeout/shutdown.
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                await process.wait()
            candidate.unlink(missing_ok=True)
            candidate.with_suffix('.json').unlink(missing_ok=True)

    async def run(self):
        while True:
            try: await self.generate_once()
            except asyncio.CancelledError: raise
            except Exception as exc:
                # Do not log upstream URLs: the proxy can contain credentials.
                LOG.warning('GTFS-RT generation failed (%s); retaining last successful feed',type(exc).__name__)
            await asyncio.sleep(self.interval)


def create_app(directory=None, *, enabled=True, producer=None):
    root=Path(directory or os.getenv('REALTIME_DIR','/realtime-data'))
    worker=producer or Producer(root,interval=float(os.getenv('GTFS_RT_INTERVAL_SECONDS','5')),
                               timeout=float(os.getenv('GTFS_RT_TIMEOUT_SECONDS','60')))
    @asynccontextmanager
    async def lifespan(app):
        root.mkdir(parents=True,exist_ok=True)
        task=None
        if enabled:
            worker.lock=(root/'producer.lock').open('a')
            try: fcntl.flock(worker.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                worker.lock.close(); raise RuntimeError('Another GTFS-RT producer is already running')
            task=asyncio.create_task(worker.run())
        try: yield
        finally:
            if task:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
            if worker.lock: worker.lock.close()
    app=FastAPI(title='Пересадочная GTFS-RT',lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if exc.status_code == 503:
            return JSONResponse(status_code=503, content={'error': {'code': 'realtime_unavailable', 'message': str(exc.detail)}})
        return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})

    def snapshot():
        try: return read_feed(root/'feed.bin')
        except Exception: raise HTTPException(503,'No successful GTFS-RT generation is available') from None

    @app.get('/health/live')
    def live(): return {'status':'ok'}

    @app.get('/health/ready')
    def ready():
        _,feed=snapshot()
        return {'status':'ready','timestamp':feed.header.timestamp,'stale':time.time()-feed.header.timestamp>90}

    @app.get('/api/v1/raw_feed',response_class=Response,responses={200:{'content':{'application/octet-stream':{'schema':{'type':'string','format':'binary'}}}},503:{'description':'No successful feed'}})
    def raw_feed():
        payload,feed=snapshot()
        age=time.time()-feed.header.timestamp
        return Response(payload,media_type='application/octet-stream',headers={'Cache-Control':'no-store',
            'X-Feed-Timestamp':str(feed.header.timestamp),'X-Feed-Stale':str(age>90 or age < -30).lower()})

    @app.get('/api/v1/vehicles')
    def vehicles(bbox:str=Query(),modes:str|None=None,limit:int=Query(default=10000,ge=1,le=20000)):
        try:
            west,south,east,north=map(float,bbox.split(','))
            if not all(math.isfinite(v) for v in (west,south,east,north)) or not (-180<=west<east<=180 and -90<=south<north<=90): raise ValueError
            accepted=set(modes.split(',')) if modes is not None else None
            if accepted is not None and not accepted <= {'bus','tram','trolleybus','metro','rail','unknown'}: raise ValueError
        except ValueError: raise HTTPException(422,'Invalid bbox or modes') from None
        _,feed=snapshot()
        try: route_modes=json.loads((root/'stats.json').read_text())['route_modes']
        except (OSError,ValueError,KeyError): route_modes={}
        items=[]
        now=time.time()
        for entity in feed.entity:
            if not entity.HasField('vehicle'): continue
            v=entity.vehicle; p=v.position
            if not v.HasField('position') or not v.HasField('timestamp'): continue
            mode=route_modes.get(v.trip.route_id,'unknown')
            if not west<=p.longitude<=east or not south<=p.latitude<=north or (accepted is not None and mode not in accepted): continue
            items.append({'vehicle_id':v.vehicle.id or entity.id.removeprefix('vehicle:'),
                'route_id':v.trip.route_id or None,'trip_id':v.trip.trip_id or None,
                'lat':p.latitude,'lon':p.longitude,'heading':p.bearing if p.HasField('bearing') else None,
                'speed_mps':p.speed if p.HasField('speed') else None,'mode':mode,
                'timestamp':datetime.fromtimestamp(v.timestamp,timezone.utc).isoformat(),'label':v.vehicle.label or None})
        stale=not -30<=now-feed.header.timestamp<=90 or any(not -30<=now-datetime.fromisoformat(i['timestamp']).timestamp()<=90 for i in items)
        warnings=['gtfs_rt_stale'] if stale else []
        if len(items)>limit: warnings.append('vehicles_truncated')
        return {'mode':'realtime','items':items[:limit],
            'updated_at':datetime.fromtimestamp(feed.header.timestamp,timezone.utc).isoformat(),
            'stale':stale,'warnings':warnings}
    return app

app=create_app()
