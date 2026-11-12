import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from fastapi.testclient import TestClient
from google.transit import gtfs_realtime_pb2 as pb
import pytest

from realtime.main import Producer, create_app


def feed(at=None):
    f=pb.FeedMessage(); f.header.gtfs_realtime_version='2.0'; f.header.timestamp=int(at or time.time())
    return f


def test_binary_and_bbox_read_same_snapshot_and_missing_bearing(tmp_path):
    f=feed()
    for i,(lon,lat,heading) in enumerate([(30.1,60.1,90),(30.2,60.2,None),(35,65,180)]):
        e=f.entity.add();e.id=str(i); v=e.vehicle
        v.vehicle.id=str(i);v.trip.route_id='route'; v.timestamp=f.header.timestamp
        v.position.latitude=lat;v.position.longitude=lon
        if heading is not None: v.position.bearing=heading
    payload=f.SerializeToString(); (tmp_path/'feed.bin').write_bytes(payload)
    (tmp_path/'stats.json').write_text(json.dumps({'route_modes':{'route':'bus'}}))
    with TestClient(create_app(tmp_path,enabled=False)) as client:
        raw=client.get('/api/v1/raw_feed')
        assert raw.content==payload and raw.headers['content-type']=='application/octet-stream'
        data=client.get('/api/v1/vehicles',params={'bbox':'30,60,31,61','limit':1,'modes':'bus'}).json()
        assert data['mode']=='realtime' and data['stale'] is False
        assert data['items'][0]['heading']==90 and 'vehicles_truncated' in data['warnings']
        data=client.get('/api/v1/vehicles',params={'bbox':'30.15,60.15,31,61'}).json()
        assert data['items'][0]['heading'] is None
        assert client.get('/api/v1/vehicles',params={'bbox':'30,60,30,61'}).status_code==422
        assert client.get('/api/v1/vehicles',params={'bbox':'nan,60,31,61'}).status_code==422


def test_no_file_503_and_stale_last_good_is_retained(tmp_path):
    with TestClient(create_app(tmp_path,enabled=False)) as client:
        assert client.get('/api/v1/raw_feed').status_code==503
        f=feed(time.time()-200);(tmp_path/'feed.bin').write_bytes(f.SerializeToString())
        assert client.get('/api/v1/raw_feed').headers['x-feed-stale']=='true'
        assert client.get('/api/v1/vehicles',params={'bbox':'30,60,31,61'}).json()['stale'] is True


@pytest.mark.parametrize('body', ['raise SystemExit(1)', 'import time;time.sleep(60)', 'from pathlib import Path;import sys;Path(sys.argv[-1]).write_bytes(b"broken")'])
def test_failed_timeout_or_invalid_candidate_keeps_previous(tmp_path,body):
    previous=feed().SerializeToString();(tmp_path/'feed.bin').write_bytes(previous)
    worker=Producer(tmp_path,timeout=.15,command=[sys.executable,'-c',body])
    with pytest.raises(Exception): asyncio.run(worker.generate_once())
    assert (tmp_path/'feed.bin').read_bytes()==previous
    assert not (tmp_path/'candidate.bin').exists()


def test_success_is_published_only_after_completion_and_http_continues(tmp_path):
    previous=feed(time.time()-10).SerializeToString();(tmp_path/'feed.bin').write_bytes(previous)
    new=feed().SerializeToString()
    script='import pathlib,sys,time; p=pathlib.Path(sys.argv[-1]);p.write_bytes('+repr(new)+');time.sleep(.15)'
    worker=Producer(tmp_path,command=[sys.executable,'-c',script])
    async def check():
        task=asyncio.create_task(worker.generate_once())
        await asyncio.sleep(.05)
        assert (tmp_path/'feed.bin').read_bytes()==previous
        await task
        assert (tmp_path/'feed.bin').read_bytes()==new
    asyncio.run(check())


def test_second_producer_cannot_start(tmp_path):
    with TestClient(create_app(tmp_path)):
        with pytest.raises(RuntimeError,match='already running'):
            with TestClient(create_app(tmp_path)): pass


def test_cancel_kills_worker(tmp_path):
    async def check():
        worker=Producer(tmp_path,command=[sys.executable,'-c','import time;time.sleep(60)'])
        task=asyncio.create_task(worker.generate_once()); await asyncio.sleep(.05);task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not (tmp_path/'candidate.bin').exists()
    asyncio.run(check())


def test_http_serves_last_good_during_running_generator(tmp_path):
    payload=feed().SerializeToString();(tmp_path/'feed.bin').write_bytes(payload)
    worker=Producer(tmp_path,command=[sys.executable,'-c','import time;time.sleep(60)'])
    with TestClient(create_app(tmp_path,producer=worker)) as client:
        start=time.monotonic()
        assert client.get('/api/v1/raw_feed').content==payload
        assert client.get('/health/live').status_code==200
        assert time.monotonic()-start<1
