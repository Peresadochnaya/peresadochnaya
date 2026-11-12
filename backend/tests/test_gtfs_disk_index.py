import concurrent.futures
import json
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest

from app.gtfs_store import GtfsStore
from app import gtfs_index
from test_gtfs_api import publish, api


@pytest.fixture(autouse=True)
def isolated_index(tmp_path, monkeypatch):
    monkeypatch.setenv('GTFS_INDEX_DIR', str(tmp_path / 'indexes'))


def test_warm_requests_never_reopen_csv_and_use_indexed_stop_lookup(tmp_path, monkeypatch):
    digest, _ = publish(tmp_path)
    gtfs_index.warm(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('Warm GTFS queries must not decompress any CSV')
    monkeypatch.setattr(zipfile.ZipFile, 'open', forbidden)
    c = api(tmp_path)
    result = c.get('/api/v1/stops/b/departures', params={
        'dummy': False, 'departure_at': '2026-11-11T00:50:00+03:00'})
    assert result.status_code == 200, result.text
    assert result.json()['items']
    assert c.get('/api/v1/trips/t1', params={'dummy': False}).status_code == 200
    assert c.get('/api/v1/stops/b', params={'dummy': False}).status_code == 200
    with gtfs_index.connect(tmp_path / 'indexes' / f'v1-{digest}.sqlite3') as db:
        plan = db.execute('EXPLAIN QUERY PLAN SELECT * FROM gtfs_stop_times WHERE stop_id=?', ('b',)).fetchall()
        assert any('gtfs_stop_times_stop_id' in row[3] for row in plan)
        plan = db.execute('EXPLAIN QUERY PLAN SELECT * FROM gtfs_calendar_dates WHERE date=?', ('20261110',)).fetchall()
        assert any('gtfs_calendar_dates_date' in row[3] for row in plan)


def test_concurrent_cold_queries_build_once(tmp_path, monkeypatch):
    publish(tmp_path)
    original = gtfs_index.build
    builds, guard = [], threading.Lock()
    def counted(*args):
        with guard:
            builds.append(1)
        return original(*args)
    monkeypatch.setattr(gtfs_index, 'build', counted)
    store = GtfsStore(tmp_path)
    def read(_):
        with store.open() as snapshot:
            return snapshot.get_route('r1').short_name
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(read, range(4))) == ['193'] * 4
    assert len(builds) == 1


def test_failed_import_does_not_publish_partial_index(tmp_path):
    path = tmp_path / 'bad.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('stops.txt', 'stop_id,stop_name\ns,Name,unexpected\n')
    digest = 'a' * 64
    with zipfile.ZipFile(path) as archive, pytest.raises(ValueError):
        with gtfs_index.open_index(archive, digest):
            pass
    assert not (tmp_path / 'indexes' / f'v1-{digest}.sqlite3').exists()
    assert not list((tmp_path / 'indexes').glob('.index-*'))


def test_index_is_invalidated_by_gtfs_version_and_old_request_remains_readable(tmp_path):
    from test_gtfs_api import tables
    old, _ = publish(tmp_path)
    store = GtfsStore(tmp_path)
    with store.open() as previous:
        t = tables(); t['routes.txt'][0]['route_short_name'] = 'NEW'
        new, _ = publish(tmp_path, t)
        with store.open() as current:
            assert current.get_route('r1').short_name == 'NEW'
        assert previous.get_route('r1').short_name == '193'
    assert old != new
    assert len(list((tmp_path / 'indexes').glob('*.sqlite3'))) == 2


def test_all_available_feeds_are_warmed_and_reused(tmp_path):
    import shutil
    digest, folder = publish(tmp_path)
    for feed in ('metro', 'rail'):
        target = tmp_path / feed / 'versions' / digest
        target.mkdir(parents=True)
        shutil.copyfile(folder / 'feed.zip', target / 'feed.zip')
        (tmp_path / feed / 'current.json').write_text(json.dumps({'sha256': digest}))
    first = gtfs_index.warm(tmp_path)
    assert {item['feed'] for item in first} == {'gtfs', 'metro', 'rail'}
    target = tmp_path / 'indexes' / f'v1-{digest}.sqlite3'
    modified = target.stat().st_mtime_ns
    gtfs_index.warm(tmp_path)
    assert target.stat().st_mtime_ns == modified


def test_startup_preloads_tables_before_first_request(tmp_path, monkeypatch):
    publish(tmp_path)
    monkeypatch.setenv('GTFS_PRELOAD', 'true')
    with api(tmp_path) as c:
        def forbidden(*args, **kwargs):
            raise AssertionError('Startup must finish importing GTFS before accepting requests')
        monkeypatch.setattr(zipfile.ZipFile, 'open', forbidden)
        assert c.get('/api/v1/transit-routes', params={'dummy': False}).status_code == 200


def test_gc_preserves_index_used_by_loaded_otp_graph(tmp_path):
    import os
    import time
    from test_gtfs_api import tables
    old,_=publish(tmp_path);gtfs_index.warm(tmp_path)
    folder=tmp_path/'otp/current';folder.mkdir(parents=True)
    (folder/'manifest.json').write_text(json.dumps({'gtfs_source_sha256':old}))
    old_index=tmp_path/'indexes'/f'v1-{old}.sqlite3'
    os.utime(old_index,(time.time()-7200,)*2)
    data=tables();data['routes.txt'][0]['route_short_name']='NEW'
    publish(tmp_path,data);gtfs_index.warm(tmp_path)
    assert old_index.exists()
