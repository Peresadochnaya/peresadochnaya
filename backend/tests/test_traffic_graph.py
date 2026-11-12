import json
import os
from pathlib import Path
from app.traffic_graph import TrafficGraph,graph_for_active
from app.gtfs_index import warm

class Snapshot:
    version='a'*64
    def __init__(self):
        self.calls=[]
        self.data={
          'trips.txt':[dict(trip_id='t',route_id='r',shape_id='s',service_id='daily')],
          'routes.txt':[dict(route_id='r',route_type='3')],
          'stop_times.txt':[dict(trip_id='t',stop_id=s,stop_sequence=str(i),arrival_time=a,departure_time=d,shape_dist_traveled=str(distance)) for i,s,a,d,distance in [(1,'a','08:00:00','08:01:00',0),(2,'b','08:05:00','08:06:00',1),(3,'c','08:10:00','08:12:00',2)]],
          'shapes.txt':[dict(shape_id='s',shape_pt_sequence=str(i),shape_pt_lon=str(30+i*.01),shape_pt_lat='60',shape_dist_traveled=str(i)) for i in range(3)],
          'frequencies.txt':[dict(trip_id='t',start_time='08:00:00',end_time='09:00:00',headway_secs='600',exact_times='0'),dict(trip_id='t',start_time='09:00:00',end_time='10:00:00',headway_secs='300',exact_times='1')],
          'stops.txt':[dict(stop_id=s,stop_lon=str(30+i*.01),stop_lat='60') for i,s in enumerate('abc')]}
    def rows(self,name,required=True,filters=None):
        self.calls.append((name,filters))
        yield from [r for r in self.data.get(name,[]) if not filters or all(r.get(k)==v for k,v in filters.items())]


def test_trip_indexed_shape_slices_motion_dwell_and_occurrences():
    snapshot=Snapshot(); graph=TrafficGraph(snapshot)
    segments=graph.trip_segments('2:t','2026-11-12','2:a','2:c')
    assert len(segments)==2
    assert segments[0].moving_seconds==240
    assert segments[0].dwell_seconds==60
    assert segments[1].dwell_seconds==0
    assert segments[0].geometry[-1]==segments[1].geometry[0]
    assert segments[0].occurrence_id!=segments[1].occurrence_id
    assert all(filters for name,filters in snapshot.calls if name=='stop_times.txt')


def test_select_frequency_window():
    graph=TrafficGraph(Snapshot())
    assert graph.service_info('t',departure_at='08:30:00')['frequency_exact_times']==0
    assert graph.service_info('t',departure_at='09:30:00')['frequency_exact_times']==1


def test_active_version_mismatch_and_unsafe_digest(tmp_path):
    folder=tmp_path/'otp'; folder.mkdir()
    (folder/'current.json').write_text(json.dumps({'version':'v1','gtfs_source_sha256':'a'*64}))
    assert graph_for_active(tmp_path,'b'*64,'v1') is None
    assert graph_for_active(tmp_path,'a'*64,'v2') is None
    (folder/'current.json').write_text(json.dumps({'gtfs_source_sha256':'../escape'}))
    assert graph_for_active(tmp_path) is None


def test_warm_preserves_only_current_graph_sources(tmp_path):
    root=tmp_path/'data'; (root/'otp').mkdir(parents=True)
    retained='a'*64; stale='b'*64
    (root/'otp'/'current.json').write_text(json.dumps({'gtfs_source_sha256':retained}))
    indexes=tmp_path/'indexes'; indexes.mkdir()
    for digest in (retained,stale):
        path=indexes/f'v1-{digest}.sqlite3'; path.write_bytes(b'old'); os.utime(path,(0,0))
    warm(root,indexes)
    assert (indexes/f'v1-{retained}.sqlite3').exists()
    assert not (indexes/f'v1-{stale}.sqlite3').exists()


def test_curated_road_status_provenance(tmp_path):
    path=tmp_path/'status.json'
    path.write_text(json.dumps({'version':'2026-11-12','edges':[dict(feed_id='gtfs',from_stop_id='a',to_stop_id='b',status='dedicated',source='https://example.test/official',date='2026-11-12')]}))
    graph=TrafficGraph(Snapshot()); graph.load_road_statuses(path)
    assert graph.trip_segments('t')[0].road_status=='dedicated'


def test_frequency_window_boarding_offset_and_unknown_window():
    graph=TrafficGraph(Snapshot())
    # Boarding B six minutes after template start still belongs to the earlier window.
    assert graph.service_info('t',departure_at='09:03:00',boarding_stop_id='b')['frequency_exact_times']==0
    assert graph.service_info('t',departure_at='12:00:00')['service_kind']=='frequency'


def test_transfer_minimum_parent_and_specific_trip_override():
    snapshot=Snapshot()
    snapshot.data['stops.txt'][0]['parent_station']='station'
    snapshot.data['transfers.txt']=[{'from_stop_id':'station','to_stop_id':'b','transfer_type':'2','min_transfer_time':'180'}, {'from_stop_id':'a','to_stop_id':'b','from_trip_id':'t','transfer_type':'2','min_transfer_time':'60'}]
    graph=TrafficGraph(snapshot)
    assert graph.transfer_minimum('a','b')['minimum_seconds']==180
    assert graph.transfer_minimum('a','b','t')['minimum_seconds']==60


def test_same_pair_variants_only_mark_auxiliary_representative_ambiguous():
    snapshot=Snapshot()
    snapshot.data['trips.txt'].append(dict(trip_id='other',route_id='r',shape_id='detour',service_id='daily'))
    snapshot.data['stop_times.txt'] += [dict(row,trip_id='other') for row in list(snapshot.data['stop_times.txt'])]
    snapshot.data['shapes.txt'] += [dict(row,shape_id='detour',shape_pt_lat='60.001') for row in list(snapshot.data['shapes.txt'])]
    graph=TrafficGraph(snapshot)
    assert not graph.trip_segments('t')[0].ambiguous
    assert not graph.trip_segments('other')[0].ambiguous
    assert not graph.trip_segments('t')[0].ambiguous
    key=('gtfs','a','b')
    assert key in graph._ambiguous_edges
    assert graph.edge_segment({'key':key,'trip_id':'t'}).ambiguous


def test_frequency_window_timezone_and_after_midnight():
    from datetime import datetime,timezone
    snapshot=Snapshot(); snapshot.data['agency.txt']=[{'agency_timezone':'Europe/Moscow'}]
    graph=TrafficGraph(snapshot)
    assert graph.service_info('t','2026-11-12',datetime(2026,11,12,6,30,tzinfo=timezone.utc))['frequency_exact_times']==1
    snapshot.data['frequencies.txt']=[dict(trip_id='t',start_time='24:00:00',end_time='26:00:00',headway_secs='300',exact_times='1')]
    assert graph.service_info('t','2026-11-12',datetime(2026,11,12,22,tzinfo=timezone.utc))['frequency_exact_times']==1


def test_composite_rejects_colliding_native_ids_and_keeps_colons():
    from app.traffic_graph import CompositeTrafficGraph
    surface=TrafficGraph(Snapshot()); metro_snapshot=Snapshot()
    metro_snapshot.data['trips.txt'][0]['trip_id']='metro:trip'
    metro_snapshot.data['frequencies.txt']=[dict(trip_id='metro:trip',start_time='00:00:00',end_time='24:00:00',headway_secs='120',exact_times='0')]
    metro=TrafficGraph(metro_snapshot,feed_id='metro')
    graph=CompositeTrafficGraph({'gtfs':surface,'metro':metro})
    assert graph.service_info('OTP:metro:trip')['headway_seconds']==120
    metro_snapshot.data['trips.txt'][0]['trip_id']='t'
    assert graph.service_info('OTP:t')['service_kind']=='unknown'


def test_trip_and_edge_geometry_caches_enforce_byte_budgets():
    graph=TrafficGraph(Snapshot())
    graph.max_trip_bytes=1; graph.max_evidence_bytes=1
    before=graph.trip_segments('t')
    assert before and not graph._trips and not graph._edge_geometries
    assert graph._trip_bytes<=1 and graph._evidence_bytes<=1
    after=graph.trip_segments('t')
    assert before==after


def test_first_large_shape_never_runs_quadratic_self_overlap(monkeypatch):
    import app.traffic_geometry as geometry
    snapshot=Snapshot()
    snapshot.data['stop_times.txt']=[snapshot.data['stop_times.txt'][0],dict(snapshot.data['stop_times.txt'][-1],shape_dist_traveled='9999')]
    snapshot.data['shapes.txt']=[dict(shape_id='s',shape_pt_sequence=str(i),shape_pt_lon=str(30+i*.000001),shape_pt_lat='60',shape_dist_traveled=str(i)) for i in range(10000)]
    def forbidden(*args,**kwargs): raise AssertionError('first occurrence must not compare its shape to itself')
    monkeypatch.setattr(geometry,'oriented_overlap',forbidden)
    graph=TrafficGraph(snapshot)
    segments=graph.trip_segments('t')
    assert len(segments)==1 and len(segments[0].geometry)==10000


def test_directed_lane_evidence_retains_provenance_and_only_excludes_one_edge(tmp_path):
    from app.traffic_geometry import correct_segment
    path=tmp_path/'lanes.json'
    path.write_text(json.dumps({'version':'official-2026','edges':[dict(feed_id='gtfs',from_stop_id='a',to_stop_id='b',status='dedicated',source='https://example.test/official',date='2026-11-12')]}))
    graph=TrafficGraph(Snapshot()); graph.load_road_statuses(path)
    first,second=graph.trip_segments('t')
    assert first.lane_source_url=='https://example.test/official'
    assert first.lane_source_observed_at.isoformat()=='2026-11-12T00:00:00+00:00'
    assert first.road_status_version=='official-2026'
    assert correct_segment(first,[],now=0).applicable_m==0
    assert correct_segment(second,[],now=0).applicable_m>0
    assert second.lane_source_url is None


def test_partial_directed_lane_excludes_only_half_with_full_length_time_baseline(tmp_path):
    from app.traffic_geometry import correct_segment,length
    from app.traffic_data import RoadObservation
    import pytest
    graph=TrafficGraph(Snapshot()); segment=graph.trip_segments('t')[0]
    total=length(segment.geometry)
    path=tmp_path/'lanes.json'
    path.write_text(json.dumps({'version':'2026','edges':[dict(feed_id='gtfs',from_stop_id='a',to_stop_id='b',status='dedicated',source='https://example.test/lanes',date='2026-11-12',spans=[dict(start_m=0,end_m=total/2)])]}))
    graph.load_road_statuses(path); segment=graph.trip_segments('t')[0]
    observation=RoadObservation('o','yandex',segment.geometry,total,480,0)
    result=correct_segment(segment,[observation],now=0)
    assert result.applicable_m==pytest.approx(total/2)
    assert result.covered_m==pytest.approx(total/2)
    # Original full-edge moving time 240; eligible half baseline120/car240.
    assert result.added_seconds==pytest.approx(120)
    assert result.fragments[0]['baseline_seconds']==pytest.approx(120)
    assert result.fragments[0]['start_m']==pytest.approx(total/2)
    assert segment.lane_spans[0]['source']=='https://example.test/lanes'
    assert 'road_status_unknown' in result.warnings


def test_overlapping_lane_spans_rejected_atomically(tmp_path):
    import pytest
    graph=TrafficGraph(Snapshot()); path=tmp_path/'lanes.json'
    path.write_text(json.dumps({'version':'2026','edges':[dict(feed_id='gtfs',from_stop_id='a',to_stop_id='b',status='dedicated',source='https://example.test',date='2026-11-12',spans=[dict(start_m=0,end_m=100),dict(start_m=50,end_m=150)])]}))
    with pytest.raises(ValueError,match='Overlapping'): graph.load_road_statuses(path)
    assert graph.trip_segments('t')[0].road_status=='unknown'


def _two_shape_snapshot():
    snapshot=Snapshot()
    snapshot.data['trips.txt'].append(dict(trip_id='other',route_id='r',shape_id='detour',service_id='daily'))
    snapshot.data['stop_times.txt'] += [dict(row,trip_id='other') for row in list(snapshot.data['stop_times.txt'])]
    snapshot.data['shapes.txt'] += [dict(row,shape_id='detour',shape_pt_lat='60.001') for row in list(snapshot.data['shapes.txt'])]
    return snapshot


def _fixed_trip_correction(graph):
    from app.traffic_data import RoadObservation
    from app.traffic_geometry import correct_segment,length
    segment=graph.trip_segments('t')[0]
    observed=RoadObservation('fixed','fixture',segment.geometry,length(segment.geometry),480,100)
    result=correct_segment(segment,[observed],now=100)
    return segment,result


def test_exact_trip_correction_survives_neighbor_bfs_and_cache_eviction():
    import pytest
    expected=_fixed_trip_correction(TrafficGraph(_two_shape_snapshot()))
    assert expected[1].added_seconds==pytest.approx(240)
    for budget in (1,24*1024*1024):
        for order in (('t','other'),('other','t')):
            graph=TrafficGraph(_two_shape_snapshot())
            graph.max_trip_bytes=budget; graph.max_evidence_bytes=budget
            for trip in order: graph.trip_segments(trip)
            key=('gtfs','a','b')
            graph.adjacency={'a':[{'key':key,'trip_id':'other','line_count':1}]}
            # The collector explores a representative, never a selected-trip baseline.
            for edge in graph.candidate_edge_neighbors([('gtfs','before','a')]):
                graph.edge_segment(edge)
            assert _fixed_trip_correction(graph)==expected
            graph.trip_segments('other')
            assert _fixed_trip_correction(graph)==expected


def test_concurrent_trip_reads_cannot_change_fixed_trip_correction():
    from concurrent.futures import ThreadPoolExecutor
    graph=TrafficGraph(_two_shape_snapshot()); graph.max_trip_bytes=1
    expected=_fixed_trip_correction(graph)
    def read(index):
        graph.trip_segments('other' if index%2 else 't')
        return _fixed_trip_correction(graph)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(result==expected for result in pool.map(read,range(24)))


def test_disk_topology_warmup_keeps_selected_trip_correction_stable(tmp_path):
    import csv,io,zipfile
    from app.gtfs_index import build
    from app.traffic_graph import _PinnedSnapshot
    snapshot=_two_shape_snapshot(); archive=tmp_path/'feed.zip'
    with zipfile.ZipFile(archive,'w') as target:
        for name,rows in snapshot.data.items():
            text=io.StringIO(); writer=csv.DictWriter(text,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
            target.writestr(name,text.getvalue())
    index=tmp_path/'index.sqlite3'
    with zipfile.ZipFile(archive) as source: build(source,index,'a'*64)
    graph=TrafficGraph(_PinnedSnapshot(index,'a'*64)); before=_fixed_trip_correction(graph)
    assert graph.warm_topology()
    for edge in graph.edges.values(): graph.edge_segment(edge)
    graph.trip_segments('other')
    assert _fixed_trip_correction(graph)==before


def test_trip_local_nonmonotonic_shape_stays_invalid_without_cache_poison():
    snapshot=_two_shape_snapshot()
    snapshot.data['shapes.txt'][1]['shape_dist_traveled']='-1'
    graph=TrafficGraph(snapshot)
    assert graph.trip_segments('t')[0].ambiguous
    assert graph.trip_segments('t')[0].geometry==()
    assert not graph.trip_segments('other')[0].ambiguous
    assert graph.trip_segments('t')[0].ambiguous
