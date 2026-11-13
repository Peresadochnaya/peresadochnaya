"""Independent numerical and concurrent regressions for the approved D decisions."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import socket
import threading

import pytest

from app.traffic_cache import TrafficCache
from app.traffic_data import RoadObservation, SegmentBaseline
from app.traffic_geometry import correct_segment, length

NOW = 10000.0
LINE = ((30.0, 60.0), (30.02, 60.0))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Traffic regression attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def segment(**updates):
    defaults = dict(feed_id='feed', gtfs_version='v1', trip_id='trip', from_stop_id='a',
        to_stop_id='b', from_sequence=1, to_sequence=2, geometry=LINE,
        moving_seconds=480, road_status='shared')
    return SegmentBaseline(**(defaults | updates))


def observation(seconds=600, geometry=LINE, **updates):
    defaults = dict(id='road', provider='yandex', geometry=geometry,
        length_m=length(geometry), duration_seconds=seconds, received_at=NOW)
    return RoadObservation(**(defaults | updates))


def test_approved_half_edge_example_preserves_uncovered_movement():
    fixture = json.loads((Path(__file__).parent / 'fixtures/traffic/partial-movement.json').read_text())
    edge = segment(geometry=fixture['geometry'], moving_seconds=fixture['original_movement_seconds'])
    road = observation(fixture['observation_seconds'], geometry=fixture['observation_geometry'])
    result = correct_segment(edge, [road], NOW)
    assert result.added_seconds == pytest.approx(fixture['expected_added_seconds'])
    assert edge.moving_seconds + result.added_seconds == pytest.approx(600)
    assert result.covered_m / result.applicable_m == pytest.approx(.5)
    assert result.fragments[0]['interpolated'] is True


@pytest.mark.parametrize('car_seconds', [0, 120, 479, 480])
def test_faster_car_observation_covers_distance_but_never_reduces_movement(car_seconds):
    result = correct_segment(segment(), [observation(car_seconds)], NOW)
    assert result.added_seconds == 0
    assert result.covered_m == pytest.approx(length(LINE))


def test_partial_road_step_time_is_allocated_once_by_length():
    # A 1200-second car step traverses twice the bus distance: only 600 seconds
    # belong to this bus edge, whose 480-second baseline gives a 120-second add.
    road = observation(1200, geometry=((30.0, 60.0), (30.04, 60.0)))
    result = correct_segment(segment(), [road], NOW)
    assert result.added_seconds == pytest.approx(120)
    assert sum(fragment['car_seconds'] for fragment in result.fragments) == pytest.approx(600)


def test_dwell_is_not_part_of_moving_baseline_or_road_increment():
    plain = correct_segment(segment(), [observation(600)], NOW)
    with_dwell = correct_segment(segment(dwell_seconds=90), [observation(600)], NOW)
    assert with_dwell.added_seconds == plain.added_seconds == pytest.approx(120)
    assert 480 + 90 + with_dwell.added_seconds == pytest.approx(690)


def test_reusing_observation_for_different_trips_recomputes_own_baseline():
    road = observation(600)
    slow = correct_segment(segment(moving_seconds=700), [road], NOW)
    fast = correct_segment(segment(trip_id='fast', moving_seconds=300), [road], NOW)
    assert slow.added_seconds == 0
    assert fast.added_seconds == pytest.approx(300)


def test_repeated_evaluation_never_compounds_and_does_not_mutate_baseline():
    edge, road = segment(), observation(600)
    assert [correct_segment(edge, [road], NOW).added_seconds for _ in range(4)] == [120] * 4
    assert edge.moving_seconds == 480


def test_repeated_loop_passages_have_same_edge_but_distinct_occurrence():
    first = segment(service_day='2026-11-12')
    later = replace(first, from_sequence=5, to_sequence=6, moving_seconds=240)
    assert first.edge_key == later.edge_key
    assert first.occurrence_id != later.occurrence_id
    assert correct_segment(first, [observation(600)], NOW).added_seconds == pytest.approx(120)
    assert correct_segment(later, [observation(600)], NOW).added_seconds == pytest.approx(360)


@pytest.mark.parametrize('geometry', [tuple(reversed(LINE)), ((30.01, 59.99), (30.01, 60.01)),
    ((30.0, 60.001), (30.02, 60.001))])
def test_reverse_crossing_and_far_parallel_road_do_not_cover_edge(geometry):
    result = correct_segment(segment(), [observation(900, geometry)], NOW)
    assert result.covered_m == 0
    assert result.added_seconds == 0
    assert result.rejected


@pytest.mark.parametrize('updates,reason', [({'ambiguous': True}, 'ambiguous_geometry'),
    ({'geometry': ()}, 'shape_unavailable'), ({'geometry': ((30,60),(30,60))}, 'zero_length'),
    ({'moving_seconds': None}, 'missing_time_baseline')])
def test_unusable_edge_reports_reason_and_never_applies(updates, reason):
    result = correct_segment(segment(**updates), [observation(900)], NOW)
    assert result.added_seconds == result.covered_m == 0
    assert reason in {item['reason'] for item in result.rejected}


@pytest.mark.parametrize('updates', [{'mode': 'metro'}, {'mode': 'rail'},
    {'mode': 'tram', 'road_status': 'segregated'}, {'road_status': 'dedicated'}])
def test_non_applicable_edges_are_removed_from_denominator(updates):
    result = correct_segment(segment(**updates), [observation(900)], NOW)
    assert result.applicable_m == result.covered_m == result.added_seconds == 0


def test_unknown_road_status_allows_correction_with_warning():
    result = correct_segment(segment(road_status='unknown'), [observation(600)], NOW)
    assert result.added_seconds == pytest.approx(120)
    assert result.warnings


def test_source_conflict_selects_geometry_before_freshness_and_never_sums():
    exact_old = observation(600, id='exact', received_at=NOW-60)
    offset_new = observation(1000, geometry=((30,60.00005),(30.02,60.00005)),
        id='offset', provider='2gis')
    result = correct_segment(segment(), [offset_new, exact_old], NOW)
    assert result.added_seconds == pytest.approx(120)
    assert {part['observation_id'] for part in result.fragments} == {'exact'}
    assert 'offset' in result.fragments[0]['conflicting_observation_ids']


def test_equal_geometry_chooses_fresher_observation_and_exact_tie_is_stable():
    old = observation(900, id='old', received_at=NOW-60)
    new = observation(600, id='new')
    assert correct_segment(segment(), [old, new], NOW).added_seconds == pytest.approx(120)
    tied = replace(old, received_at=NOW)
    a = correct_segment(segment(), [new, tied], NOW)
    b = correct_segment(segment(), [tied, new], NOW)
    assert a.fragments == b.fragments
    assert a.fragments[0]['selection_order']


def test_overlapping_observations_and_alternatives_do_not_double_cover():
    roads = [observation(600, id='a'), observation(600, id='b', provider='2gis'),
             observation(600, id='alternative', alternative_index=1)]
    result = correct_segment(segment(), roads, NOW)
    assert result.covered_m == pytest.approx(length(LINE))
    assert result.added_seconds == pytest.approx(120)


@pytest.mark.parametrize('age,usable', [(0, True), (1799.999, True), (1800, False), (1801, False), (-1, False)])
def test_observation_freshness_has_fixed_boundary_and_rejects_future(age, usable):
    road = observation(received_at=NOW-age)
    result = correct_segment(segment(), [road], NOW)
    assert (result.covered_m > 0) is usable


def test_cache_reads_do_not_extend_thirty_minute_ttl():
    clock = [NOW]
    cache = TrafficCache(clock=lambda: clock[0])
    cache.put('edge', [observation()])
    for age in [60, 1200, 1799.999]:
        clock[0] = NOW + age
        assert cache.get('edge')
    clock[0] = NOW + 1800
    assert cache.get('edge') is None


def test_cache_respects_observation_expiry_instead_of_insertion_time():
    cache = TrafficCache(clock=lambda: NOW)
    cache.put('old', [observation(received_at=NOW-1790)])
    cache.clock = lambda: NOW+10
    assert cache.get('old') is None


def test_cache_evicts_least_recently_used_and_bounds_bytes():
    cache = TrafficCache(max_entries=2, max_bytes=1000, clock=lambda: NOW)
    cache.put('a', 'one'); cache.put('b', 'two')
    assert cache.get('a') == 'one'
    cache.put('c', 'three')
    assert cache.get('b') is None
    assert cache.get('a') == 'one'
    cache.put('huge', 'x'*2000)
    assert cache.get('huge') is None
    assert cache.size_bytes <= 1000


def test_cache_same_key_concurrent_requests_share_one_load():
    cache = TrafficCache(clock=lambda: NOW)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def loader():
        calls.append(1); entered.set()
        assert release.wait(2)
        return [observation()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(cache.get_or_load, 'same', loader)
        assert entered.wait(2)
        others = [pool.submit(cache.get_or_load, 'same', loader) for _ in range(7)]
        release.set()
        results = [future.result(timeout=2) for future in [first, *others]]
    assert calls == [1]
    assert all(value == results[0] for value in results)


def test_cache_loader_failure_can_be_retried_and_never_cached():
    cache = TrafficCache(clock=lambda: NOW)
    def broken():
        raise ValueError('offline failure')
    with pytest.raises(ValueError, match='offline failure'):
        cache.get_or_load('key', broken)
    assert cache.get('key') is None
    assert cache.get_or_load('key', lambda: 'recovered') == 'recovered'


def test_singleflight_waiter_honors_own_timeout_without_canceling_owner():
    from concurrent.futures import TimeoutError
    cache=TrafficCache(clock=lambda:NOW)
    entered,release=threading.Event(),threading.Event()
    def loader():
        entered.set(); assert release.wait(2)
        return 'complete'
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner=pool.submit(cache.get_or_load,'shared',loader)
        assert entered.wait(2)
        try:
            with pytest.raises(TimeoutError):
                cache.get_or_load('shared',lambda:pytest.fail('duplicate load'),timeout=.01)
        finally:
            release.set()
        assert owner.result(timeout=2)=='complete'
    assert cache.get('shared')=='complete'


def indexed_graph(tmp_path,monkeypatch,data=None):
    from test_gtfs_api import publish
    from app import gtfs_index
    from app.traffic_graph import graph_for_active
    monkeypatch.setenv('GTFS_INDEX_DIR',str(tmp_path/'indexes'))
    digest,_=publish(tmp_path,data)
    gtfs_index.warm(tmp_path)
    otp=tmp_path/'otp'; otp.mkdir(exist_ok=True)
    (otp/'current.json').write_text(json.dumps({'version':'g1','gtfs_source_sha256':digest}))
    return graph_for_active(tmp_path,digest,'g1'),digest


def test_graph_uses_existing_selected_trip_index_without_csv_or_full_stop_times_scan(tmp_path,monkeypatch):
    import zipfile
    from app import gtfs_index
    graph,digest=indexed_graph(tmp_path,monkeypatch)
    reads=[]; actual=gtfs_index.DiskSnapshot.rows
    def guarded(self,name,required=True,filters=None):
        reads.append((name,filters))
        if name=='stop_times.txt':
            assert filters and filters.get('trip_id')=='t1'
        yield from actual(self,name,required,filters)
    monkeypatch.setattr(gtfs_index.DiskSnapshot,'rows',guarded)
    monkeypatch.setattr(zipfile.ZipFile,'open',lambda *a,**k:pytest.fail('traffic must not read CSV'))
    result=graph.trip_segments('t1',service_day='2026-11-10',from_stop_id='a',to_stop_id='b')
    assert len(result)==1
    assert result[0].moving_seconds==1200
    assert result[0].gtfs_version==digest
    assert result[0].geometry[0]==pytest.approx((30.1,59.9))
    assert result[0].geometry[-1]==pytest.approx((30.2,60.0))
    assert any(name=='stop_times.txt' for name,filters in reads)


def test_active_graph_rejects_version_mismatch_and_never_uses_latest(tmp_path,monkeypatch):
    from app.traffic_graph import graph_for_active
    graph,digest=indexed_graph(tmp_path,monkeypatch)
    assert graph is not None
    assert graph_for_active(tmp_path,digest,'wrong-graph') is None
    assert graph_for_active(tmp_path,'b'*64,'g1') is None
    # New GTFS pointer cannot change the retained active graph's baseline.
    (tmp_path/'gtfs/current.json').write_text(json.dumps({'sha256':'b'*64}))
    assert graph_for_active(tmp_path,digest,'g1').version==digest


def test_mixed_frequency_windows_select_own_exact_times_and_headway(tmp_path,monkeypatch):
    from test_gtfs_api import tables
    from datetime import datetime
    data=tables()
    data['frequencies.txt']=[
        {'trip_id':'t1','start_time':'08:00:00','end_time':'09:00:00','headway_secs':'300','exact_times':'0'},
        {'trip_id':'t1','start_time':'09:00:00','end_time':'10:00:00','headway_secs':'600','exact_times':'1'}]
    graph,_=indexed_graph(tmp_path,monkeypatch,data)
    early=graph.service_info('t1',service_day='2026-11-12',departure_at=datetime.fromisoformat('2026-11-12T08:30:00+03:00'))
    boundary=graph.service_info('t1',service_day='2026-11-12',departure_at=datetime.fromisoformat('2026-11-12T09:00:00+03:00'))
    assert early['frequency_exact_times']==0 and early['headway_seconds']==300
    assert boundary['frequency_exact_times']==1 and boundary['headway_seconds']==600


def test_loop_stop_ids_require_sequence_instead_of_silently_selecting_first_passage(tmp_path,monkeypatch):
    from test_gtfs_api import tables
    data=tables()
    data['stop_times.txt']=[
        {'trip_id':'t1','stop_id':stop,'arrival_time':time,'departure_time':time,'stop_sequence':str(i)}
        for i,(stop,time) in enumerate([('a','08:00:00'),('b','08:05:00'),('a','08:10:00'),('b','08:20:00')])]
    graph,_=indexed_graph(tmp_path,monkeypatch,data)
    assert graph.trip_segments('t1',from_stop_id='a',to_stop_id='b')==[]
    later=graph.trip_segments('t1',from_stop_id='a',to_stop_id='b',from_sequence=2,to_sequence=3)
    assert len(later)==1 and later[0].moving_seconds==600
    assert later[0].from_sequence==2
