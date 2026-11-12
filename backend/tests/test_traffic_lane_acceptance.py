"""Independent offline acceptance of directed lane evidence and correction math."""
from dataclasses import replace
import json

import pytest

from app.traffic_data import RoadObservation, SegmentBaseline
from app.traffic_geometry import correct_segment, length
from app.traffic_graph import TrafficGraph


LINE = ((30.0, 60.0), (30.01, 60.0))


def baseline(**changes):
    return replace(SegmentBaseline('gtfs', 'a' * 64, 't', 'a', 'b', 1, 2,
                                   LINE, 480), **changes)


def observation(geometry=LINE):
    return RoadObservation('recorded-road', 'fixture', geometry, length(geometry), 960, 100)


def test_partial_lane_preserves_full_edge_time_density_and_unknown_remainder():
    size = length(LINE)
    segment = baseline(lane_spans=({'start_m': 0, 'end_m': size / 2,
                                    'status': 'dedicated'},))
    result = correct_segment(segment, [observation()], now=100)
    assert result.applicable_m == pytest.approx(size / 2)
    assert result.covered_m == pytest.approx(size / 2)
    assert result.added_seconds == pytest.approx(240)
    assert sum(f['baseline_seconds'] for f in result.fragments) == pytest.approx(240)
    assert 'road_status_unknown' in result.warnings


@pytest.mark.parametrize('geometry', [tuple(reversed(LINE)),
    ((30.005, 59.999), (30.005, 60.001))])
def test_reverse_travel_and_crossing_do_not_establish_coverage(geometry):
    result = correct_segment(baseline(), [observation(geometry)], now=100)
    assert result.covered_m == 0
    assert result.added_seconds == 0
    assert result.applicable_m == pytest.approx(length(LINE))


@pytest.mark.parametrize('spans', [
    ({'start_m': 0, 'end_m': 200, 'status': 'dedicated'},
     {'start_m': 100, 'end_m': 250, 'status': 'dedicated'}),
    ({'start_m': 0, 'end_m': 10000, 'status': 'dedicated'},),
    ({'start_m': 0, 'end_m': float('nan'), 'status': 'dedicated'},),
])
def test_invalid_lane_profile_cannot_remove_distance_from_quality_denominator(spans):
    result = correct_segment(baseline(lane_spans=spans), [observation()], now=100)
    assert result.applicable_m == pytest.approx(length(LINE))
    assert result.covered_m == 0
    assert result.added_seconds == 0
    assert 'lane_profile_rejected' in result.warnings


def test_status_replacement_is_atomic_when_a_later_record_is_invalid(tmp_path):
    graph = TrafficGraph(type('Snapshot', (), {'version': 'a' * 64})())
    path = tmp_path / 'status.json'
    valid = {'feed_id': 'gtfs', 'from_stop_id': 'a', 'to_stop_id': 'b',
             'status': 'dedicated', 'source': 'https://example.test/official',
             'date': '2026-11-12'}
    path.write_text(json.dumps({'version': 'accepted', 'edges': [valid]}))
    graph.load_road_statuses(path)
    before = dict(graph.road_statuses)
    path.write_text(json.dumps({'version': 'bad', 'edges': [
        dict(valid, from_stop_id='c'), dict(valid, status='invented')]}))
    with pytest.raises(ValueError):
        graph.load_road_statuses(path)
    assert graph.road_status_version == 'accepted'
    assert graph.road_statuses == before


@pytest.mark.parametrize('tags', [
    {'highway': 'primary', 'oneway': 'yes', 'bus': 'yes'},
    {'highway': 'primary', 'surface': 'asphalt', 'railway': 'tram'},
    {'highway': 'primary', 'bus:lanes': 'designated|yes'},
    {'highway': 'primary', 'oneway': 'yes', 'bus:lanes:conditional': 'designated|yes @ (Mo-Fr 07:00-10:00)'},
    {'highway': 'primary', 'oneway': 'yes', 'busway': 'lane', 'busway:conditional': 'no @ (Sa-Su)'},
    {'highway': 'busway', 'oneway': 'yes', 'opening_hours': 'Mo-Fr 07:00-10:00'},
])
def test_unproven_and_time_restricted_tags_cannot_be_unconditional_dedicated(tags):
    from app.traffic_lane_builder import dedicated_directions
    assert dedicated_directions(tags) == ()


def test_reverse_oneway_cannot_gain_forward_lane_from_right_side_tag():
    from app.traffic_lane_builder import dedicated_directions
    assert 1 not in dedicated_directions({'oneway': '-1', 'busway:right': 'lane'})


@pytest.mark.parametrize('variant', ['reverse', 'crossing', 'equidistant_parallel'])
def test_lane_builder_cannot_infer_carriageway_from_unrelated_geometry(variant):
    from app.traffic_lane_builder import match_spans
    def lane(identifier, geometry):
        return {'osm_way_id': identifier, 'osm_version': 1, 'geometry': geometry,
                'direction': 1, 'source': f'https://www.openstreetmap.org/way/{identifier}/history',
                'date': '2026-11-12T00:00:00+00:00'}
    if variant == 'reverse':
        lanes = [lane(1, tuple(reversed(LINE)))]
    elif variant == 'crossing':
        lanes = [lane(1, ((30.005, 59.999), (30.005, 60.001)))]
    else:
        lanes = [lane(1, tuple((x, y + .00005) for x, y in LINE)),
                 lane(2, tuple((x, y - .00005) for x, y in LINE))]
    assert match_spans(LINE, lanes) == []


def test_geometry_identity_is_directed_and_preserves_bends():
    from app.traffic_lane_builder import geometry_fingerprint
    assert geometry_fingerprint(LINE) != geometry_fingerprint(tuple(reversed(LINE)))
    assert geometry_fingerprint(LINE) != geometry_fingerprint((LINE[0], (30.005, 60.001), LINE[1]))
    assert geometry_fingerprint(LINE) == geometry_fingerprint(list(LINE))


@pytest.mark.parametrize('mutation', ['source', 'geometry', 'duplicate'])
def test_pinned_profile_rejects_source_change_geometry_change_and_duplicate(tmp_path, mutation):
    from test_traffic_graph import Snapshot
    from app.traffic_lane_builder import geometry_fingerprint
    graph = TrafficGraph(Snapshot())
    segment = graph.trip_segments('t')[0]
    row = {'feed_id': 'gtfs', 'from_stop_id': 'a', 'to_stop_id': 'b',
           'status': 'dedicated', 'source': 'https://example.test/official',
           'date': '2026-11-12', 'geometry_sha256': geometry_fingerprint(segment.geometry)}
    profile = {'version': 'pinned', 'feed_sha256': {'gtfs': graph.version}, 'edges': [row]}
    if mutation == 'source':
        profile['feed_sha256']['gtfs'] = 'b' * 64
    elif mutation == 'geometry':
        row['geometry_sha256'] = geometry_fingerprint(tuple(reversed(segment.geometry)))
    else:
        profile['edges'].append(dict(row))
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(profile))
    if mutation in ('source', 'duplicate'):
        with pytest.raises(ValueError):
            graph.load_road_statuses(path)
        assert not graph.road_statuses
    else:
        graph.load_road_statuses(path)
        actual = graph.trip_segments('t')[0]
        assert actual.road_status == 'unknown'
        assert actual.lane_spans == ()
        assert actual.lane_source_url is None


def test_optional_override_wins_and_invalid_override_falls_back_to_unknown(tmp_path, monkeypatch):
    import app.traffic_graph as module
    from test_traffic_graph import Snapshot
    digest = 'a' * 64
    (tmp_path / 'otp').mkdir()
    (tmp_path / 'otp' / 'current.json').write_text(json.dumps({'gtfs_source_sha256': digest}))
    monkeypatch.setattr(module, 'index_directory', lambda: tmp_path)
    monkeypatch.setattr(module, 'valid', lambda path, version: True)
    monkeypatch.setattr(module, '_PinnedSnapshot', lambda path, version: Snapshot())
    monkeypatch.setattr(module, '_GRAPHS', {})
    # The runtime override has deliberate precedence over the bundled layer.
    override = tmp_path / 'traffic-road-status.json'
    override.write_text(json.dumps({'version': 'user-override', 'edges': [{
        'feed_id': 'gtfs', 'from_stop_id': 'a', 'to_stop_id': 'b',
        'status': 'shared', 'source': 'https://example.test/review', 'date': '2026-11-12'}]}))
    # OrderedDict is required by the graph cache eviction contract.
    from collections import OrderedDict
    monkeypatch.setattr(module, '_GRAPHS', OrderedDict())
    graph = module.graph_for_active(tmp_path)
    assert graph.trip_segments('t')[0].road_status == 'shared'
    assert graph.road_status_version == 'user-override'
    monkeypatch.setattr(module, '_GRAPHS', OrderedDict())
    override.write_text('{malformed')
    graph = module.graph_for_active(tmp_path)
    assert graph is not None
    assert graph.trip_segments('t')[0].road_status == 'unknown'


@pytest.mark.parametrize(('tags', 'expected'), [
    ({'oneway': 'yes', 'busway': 'opposite_lane'}, (-1,)),
    ({'oneway': 'yes', 'busway:left': 'lane'}, (1,)),
    ({'oneway': '-1', 'busway:right': 'lane'}, (-1,)),
    ({'oneway': '-1', 'busway:left': 'lane'}, (-1,)),
])
def test_explicit_side_and_counterflow_preserve_osm_travel_semantics(tags, expected):
    from app.traffic_lane_builder import dedicated_directions
    assert dedicated_directions(tags) == expected


def test_real_bundled_profile_is_populated_pinned_and_traces_every_span_to_source_evidence():
    from pathlib import Path
    import re
    root = Path(__file__).resolve().parents[1] / 'app' / 'fixtures' / 'traffic'
    profile = json.loads((root / 'road-status.json').read_text())
    geometry = json.loads((root / 'osm-bus-lanes.geojson').read_text())
    assert profile['edges'], 'Production lane layer must contain real directed edges'
    assert re.fullmatch('[0-9a-f]{64}', profile['feed_sha256']['gtfs'])
    assert re.fullmatch('[0-9a-f]{64}', profile['osm_sha256'])
    assert len(geometry['features']) == profile['summary']['osm_directed_ways']
    evidence_features = list(geometry['features'])
    if profile.get('supplemental_sha256'):
        import hashlib
        supplemental = Path(__file__).resolve().parents[2] / 'research/traffic-correction/bus-lanes/official-resolved-lanes.geojson'
        assert hashlib.sha256(supplemental.read_bytes()).hexdigest() == profile['supplemental_sha256']
        official = json.loads(supplemental.read_text())
        assert len(official['features']) == profile['summary']['official_directed_fragments']
        evidence_features.extend(official['features'])
    evidence = {(f['properties']['osm_way_id'], f['properties']['direction'], f['properties']['source']): f
                for f in evidence_features}
    keys = set()
    total = 0
    for row in profile['edges']:
        key = row['feed_id'], row['from_stop_id'], row['to_stop_id']
        assert key not in keys
        keys.add(key)
        assert re.fullmatch('[0-9a-f]{64}', row['geometry_sha256'])
        assert row['status'] == 'unknown', 'Unspecified remainder must stay unknown'
        for variant in row.get('variants', [row]):
            end = 0
            for span in variant['spans']:
                assert span['start_m'] >= end
                assert span['end_m'] > span['start_m']
                end = span['end_m']
                feature = evidence[span['osm_way_id'], span['direction'], span['source']]
                assert span['source'] == feature['properties']['source']
                assert span['date'] == feature['properties']['date']
                assert span['osm_version'] == feature['properties']['osm_version']
                assert len(feature['geometry']['coordinates']) >= 2
                total += span['end_m'] - span['start_m']
    assert len(keys) == profile['summary']['directed_edges']
    assert total == pytest.approx(profile['summary']['dedicated_m'])


def test_every_official_item_is_explicitly_accounted_without_guessing_complete_coverage():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / 'research' / 'traffic-correction' / 'bus-lanes'
    inventory = json.loads((root / 'corridors.json').read_text())
    report = json.loads((root / 'official-coverage.json').read_text())
    expected = {r['id'] for group in ('corridors', 'supplemental_corridors')
                for r in inventory[group]}
    assert {r['id'] for r in report['items']} == expected
    assert len(report['items']) == len(expected) == report['official_items']
    assert report['mapped_items'] + report['unmapped_items'] == len(expected)
    for row in report['items']:
        assert row['reason']
        if row['mapping_status'] == 'not_geometrically_resolved':
            assert row['mapped_directed_edges'] is None


def test_geometry_variants_apply_only_the_matching_directed_shape(tmp_path):
    from test_traffic_graph import Snapshot
    from app.traffic_lane_builder import geometry_fingerprint
    graph = TrafficGraph(Snapshot())
    segment = graph.trip_segments('t')[0]
    source = 'https://example.test/official'
    row = {'feed_id': 'gtfs', 'from_stop_id': 'a', 'to_stop_id': 'b',
           'status': 'unknown', 'source': source, 'date': '2026-11-12',
           'geometry_sha256': geometry_fingerprint(tuple(reversed(segment.geometry))),
           'spans': [{'start_m': 0, 'end_m': 200, 'status': 'dedicated'}]}
    row['variants'] = [dict(geometry_sha256=row['geometry_sha256'], source=source,
                            date='2026-11-12', spans=row['spans']),
                       dict(geometry_sha256=geometry_fingerprint(segment.geometry),
                            source=source, date='2026-11-12',
                            spans=[{'start_m': 20, 'end_m': 40, 'status': 'dedicated'}])]
    path = tmp_path / 'variants.json'
    path.write_text(json.dumps({'version': 'variants', 'feed_sha256': {'gtfs': graph.version}, 'edges': [row]}))
    graph.load_road_statuses(path)
    actual = graph.trip_segments('t')[0]
    assert len(actual.lane_spans) == 1
    assert actual.lane_spans[0]['start_m'] == 20
    assert actual.lane_spans[0]['end_m'] == 40
    assert actual.road_status == 'unknown'


@pytest.mark.parametrize('malformation', ['duplicate', 'negative_span', 'invalid_date'])
def test_malformed_geometry_variant_is_rejected_before_atomic_install(tmp_path, malformation):
    from app.traffic_lane_builder import geometry_fingerprint
    graph = TrafficGraph(type('Snapshot', (), {'version': 'a' * 64})())
    variant = {'geometry_sha256': geometry_fingerprint(LINE),
               'source': 'https://example.test/official', 'date': '2026-11-12',
               'spans': [{'start_m': 0, 'end_m': 50, 'status': 'dedicated'}]}
    variants = [variant]
    if malformation == 'duplicate':
        variants.append(dict(variant))
    elif malformation == 'negative_span':
        variant['spans'][0]['start_m'] = -1
    else:
        variant['date'] = 'not-a-date'
    row = {'feed_id': 'gtfs', 'from_stop_id': 'a', 'to_stop_id': 'b',
           'status': 'unknown', 'source': 'https://example.test/official',
           'date': '2026-11-12', 'geometry_sha256': geometry_fingerprint(LINE),
           'variants': variants}
    path = tmp_path / 'invalid-variants.json'
    path.write_text(json.dumps({'version': 'invalid', 'feed_sha256': {'gtfs': graph.version}, 'edges': [row]}))
    with pytest.raises(ValueError):
        graph.load_road_statuses(path)
    assert graph.road_status_version is None
    assert graph.road_statuses == {}


def test_official_corridor_endpoints_limit_coverage_without_extending_to_whole_street():
    from app.traffic_lane_builder import match_spans
    from app.traffic_geometry import slice_line
    size = length(LINE)
    clipped = slice_line(LINE, size * .25, size * .75)
    official = {'osm_way_id':1234, 'osm_version':2, 'geometry':clipped,
        'source':'https://www.gov.spb.ru/gov/otrasl/c_transport/vydelennye-polosy-dlya-obshestvennogo-transporta/',
        'date':'2025-04-14', 'direction':1, 'source_kind':'official_inventory',
        'official_corridor_id':'bounded-official-fixture',
        'boundary_provenance':'Explicitly resolved two intersection endpoints'}
    spans = match_spans(LINE, [official])
    assert len(spans) == 1
    assert spans[0]['start_m'] == pytest.approx(size * .25)
    assert spans[0]['end_m'] == pytest.approx(size * .75)
    assert spans[0]['official_corridor_id'] == 'bounded-official-fixture'
    assert spans[0]['boundary_provenance'] == official['boundary_provenance']
    assert match_spans(tuple(reversed(LINE)), [official]) == []


def test_real_malaya_morskaya_official_geometry_is_bounded_unique_contraflow():
    from pathlib import Path
    from app.traffic_lane_builder import load_supplemental, match_spans
    path = Path(__file__).resolve().parents[1] / 'app/fixtures/traffic/official-resolved-lanes.geojson'
    lanes = [r for r in load_supplemental(path) if r['official_corridor_id'] == 'spb-official-045']
    assert len(lanes) == 6
    assert len({r['osm_way_id'] for r in lanes}) == 6
    assert all(r['direction'] == -1 and r['tags']['oneway'] == 'yes' for r in lanes)
    assert all(r['tags']['highway'] == 'tertiary' for r in lanes)
    combined = []
    for lane in lanes:
        if combined:
            assert combined[-1] == lane['geometry'][0]
        combined.extend(lane['geometry'] if not combined else lane['geometry'][1:])
    assert combined[0] == [30.308106, 59.9339456]
    assert combined[-1] == [30.3151542, 59.9366657]
    assert length(combined) == pytest.approx(499.6802211374, abs=.01)
    assert all(r['boundary_provenance']['path_count'] == 1 for r in lanes)
    # Deliberately extend the GTFS-like line past both official intersections.
    # Only the source-clipped corridor may become protected.
    first, second, penultimate, last = combined[0], combined[1], combined[-2], combined[-1]
    before = [first[0] - (second[0]-first[0]), first[1] - (second[1]-first[1])]
    after = [last[0] + (last[0]-penultimate[0]), last[1] + (last[1]-penultimate[1])]
    extended = [before] + combined + [after]
    spans = match_spans(extended, lanes)
    assert spans
    start = length([before, first])
    assert min(s['start_m'] for s in spans) == pytest.approx(start, abs=.01)
    assert max(s['end_m'] for s in spans) == pytest.approx(start + length(combined), abs=.01)
    assert match_spans(tuple(reversed(extended)), lanes) == []


def test_exact_trip_correction_is_invariant_to_collector_bfs_loading_other_trip_shapes():
    from types import SimpleNamespace
    from test_traffic_graph import Snapshot
    from app.traffic_cache import TrafficCache
    from app.traffic_planner import ObservationCollector
    snapshot = Snapshot()
    snapshot.data['trips.txt'].append(dict(trip_id='other', route_id='r', shape_id='detour', service_id='daily'))
    snapshot.data['stop_times.txt'] += [dict(row, trip_id='other') for row in list(snapshot.data['stop_times.txt'])]
    snapshot.data['shapes.txt'] += [dict(row, shape_id='detour', shape_pt_lat='60.001')
                                   for row in list(snapshot.data['shapes.txt'])]
    graph = TrafficGraph(snapshot)
    graph.topology_ready = True
    neighbor = {'key':('gtfs','b','c'), 'trip_id':'other', 'from_sequence':2,
                'line_count':1, 'lines':{'r'}}
    graph.adjacency = {'b':[neighbor]}
    segment_before = graph.trip_segments('t',from_stop_id='a',to_stop_id='b')[0]
    before = correct_segment(segment_before, [observation()], now=100)
    route = SimpleNamespace(legs=[SimpleNamespace(mode='bus', line=SimpleNamespace(trip_id='t'),
        origin=SimpleNamespace(stop_id='a'), destination=SimpleNamespace(stop_id='b'))])
    class RecordedRoads:
        calls = 0
        def estimate_observations(self, points, timeout_seconds=8):
            self.calls += 1
            return [observation()]
    roads = RecordedRoads()
    collector = ObservationCollector(roads, None, cache=TrafficCache(clock=lambda:100),
                                    clock=lambda:0, wall_clock=lambda:100)
    collector([route], graph, deadline=15)
    assert roads.calls == 1
    assert 'other' in graph._trips, 'Actual collector BFS must load the conflicting neighboring trip'
    after_segment = graph.trip_segments('t',from_stop_id='a',to_stop_id='b')[0]
    after = correct_segment(after_segment, [observation()], now=100)
    assert not after_segment.ambiguous
    assert after.covered_m == pytest.approx(before.covered_m)
    assert after.added_seconds == pytest.approx(before.added_seconds)
    assert after.applicable_m == pytest.approx(before.applicable_m)
    # Auxiliary representative selection still honestly reports ambiguity.
    assert graph.edge_segment(neighbor).ambiguous
