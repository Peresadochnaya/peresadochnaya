"""Independent regression examples from the traffic implementation review."""
from datetime import datetime
import pytest

from app.traffic_geometry import oriented_overlap
from app.traffic_graph import TrafficGraph
from test_traffic_graph import Snapshot


def test_road_fragments_must_follow_the_bus_order_globally():
    bus = ((0, 0), (.001, 0), (.002, 0))
    # Both eastbound fragments exist, but the car visits B-C before A-B.
    road = ((.001, 0), (.002, 0), (.002, .001), (0, .001),
            (0, 0), (.001, 0))
    assert oriented_overlap(bus, road) == []


def test_frequency_window_is_invariant_under_timestamp_timezone():
    snapshot = Snapshot()
    snapshot.data['agency.txt'] = [
        {'agency_id': 'agency', 'agency_timezone': 'Europe/Moscow'}]
    snapshot.data['routes.txt'][0]['agency_id'] = 'agency'
    graph = TrafficGraph(snapshot)
    local = graph.service_info('t', service_day='2026-11-12',
        departure_at=datetime.fromisoformat('2026-11-12T09:30:00+03:00'))
    utc = graph.service_info('t', service_day='2026-11-12',
        departure_at=datetime.fromisoformat('2026-11-12T06:30:00+00:00'))
    assert local['frequency_exact_times'] == 1
    assert utc == local


def test_first_edge_geometry_does_not_run_quadratic_self_comparison(monkeypatch):
    from app import traffic_geometry

    def unexpected_comparison(*args, **kwargs):
        raise AssertionError('A first edge has no previous geometry to compare')

    monkeypatch.setattr(traffic_geometry, 'oriented_overlap', unexpected_comparison)
    assert len(TrafficGraph(Snapshot()).trip_segments('t')) == 2


@pytest.mark.parametrize('frequency', [False, True])
def test_two_recoveries_correct_each_new_ride_once_with_one_collection(frequency):
    from test_traffic_integration import (Graph, Planner, leg, route, request,
        response, road, START, NOW, LINE, assert_timeline)
    from app.traffic_data import SegmentBaseline
    from app.traffic_engine import TrafficEngine
    from app.traffic_planner import ObservationCollector
    from app.traffic_cache import TrafficCache

    initial = route(legs=[leg(duration=60),
        leg('old-second', start=90, duration=60, origin='b', destination='c'),
        leg('old-third', start=180, duration=60, origin='c', destination='d')])
    tail_one = route('tail-one', legs=[
        leg('second', start=150, duration=60, origin='b', destination='c'),
        leg('old-third', start=240, duration=60, origin='c', destination='d')])
    tail_two = route('tail-two', legs=[
        leg('third', start=300, duration=60, origin='c', destination='d')])
    planner = Planner([initial])

    def plan(req, **kwargs):
        planner.calls.append((req.model_copy(deep=True), kwargs))
        return response([[initial], [tail_one], [tail_two]][len(planner.calls)-1])

    planner.plan = plan
    baselines = {trip: [SegmentBaseline('feed', 'v1', trip, a, b, 1, 2,
        LINE, 60, road_status='shared')] for trip, a, b in
        [('first', 'a', 'b'), ('second', 'b', 'c'), ('third', 'c', 'd')]}
    graph = Graph(frequency=frequency, baselines=baselines)

    class Provider:
        def __init__(self, values): self.values, self.calls = values, 0
        def estimate_observations(self, points, timeout_seconds):
            self.calls += 1
            return self.values

    dgis, yandex = Provider([]), Provider([road(120)])
    collector = ObservationCollector(yandex, dgis, cache=TrafficCache(clock=lambda: NOW),
        clock=lambda: 0, wall_clock=lambda: NOW)
    result = TrafficEngine(planner, graph_loader=lambda **kw: graph,
        collector=collector, clock=lambda: 0, wall_clock=lambda: NOW).plan(
            request(max_transfers=2))
    assert len(planner.calls) == 3
    assert result.debug.otp_replanning_count == 2
    value = result.routes[0]
    rides = [x for x in value.legs if x.line]
    assert [x.line.trip_id for x in rides] == ['first', 'second', 'third']
    assert [x.duration_seconds for x in rides] == [120, 120, 120]
    assert [x.original_duration_seconds for x in rides] == [60, 60, 60]
    assert [int((r.departure_at-START).total_seconds()) for r, kw in planner.calls[1:]] == [120, 270]
    assert [r.max_transfers for r, kw in planner.calls[1:]] == [1, 0]
    assert value.duration_seconds == 420
    assert_timeline(value)
    assert [x.duration_seconds for x in initial.legs] == [60, 60, 60]
    assert [x.duration_seconds for x in tail_one.legs] == [60, 60]
    assert tail_two.legs[0].duration_seconds == 60
    assert dgis.calls == yandex.calls == 1
    assert sum(c.billable_units for c in result.debug.provider_calls if c.provider != 'otp') == 2


def test_neighborhood_quota_prioritizes_depth_lines_then_proximity():
    from test_traffic_integration import route, NOW
    from app.traffic_data import SegmentBaseline
    from app.traffic_cache import TrafficCache
    from app.traffic_planner import ObservationCollector

    def segment(key, lon):
        return SegmentBaseline('feed', 'v1', key, key+'-a', key+'-b', 1, 2,
            ((lon, 60), (lon+.0001, 60)), 60, road_status='shared')

    candidates = [segment(f'candidate{i:02}', 30+i*.001) for i in range(23)]
    high = segment('high', 31)
    far = segment('a-far', 32)
    near = segment('z-near', 31.0002)
    deep = segment('depth2', 31.0003)
    neighbors = [dict(key=s.edge_key, depth=d, line_count=n, segment=s)
        for s, d, n in [(far, 1, 5), (deep, 2, 100), (near, 1, 5), (high, 1, 6)]]

    class Graph:
        topology_ready = True
        def trip_segments(self, *args, **kwargs): return candidates
        def candidate_edge_neighbors(self, *args, **kwargs): return neighbors
        def edge_segment(self, edge): return edge['segment']

    class Provider:
        def __init__(self): self.calls = []
        def estimate_observations(self, points, timeout_seconds):
            self.calls.append(points)
            return []

    dgis, yandex = Provider(), Provider()
    _, calls = ObservationCollector(yandex, dgis, cache=TrafficCache(clock=lambda: NOW),
        clock=lambda: 0, wall_clock=lambda: NOW)([route()], Graph(), 15)
    points = yandex.calls[0]
    assert len(points) == 50
    assert points[-4:] == [*high.geometry, *near.geometry]
    assert len(dgis.calls) == len(yandex.calls) == 1
    debug = next(c for c in calls if c.provider == 'yandex')
    assert len(debug.waypoint_coordinates) == 50
    assert set(debug.target_edge_ids) == {
        ':'.join(s.edge_key) for s in [*candidates, high, near]}
    assert len(debug.connectors) == 24
    assert debug.connectors[-1].coordinates == [high.geometry[-1], near.geometry[0]]
    assert not debug.unrepresented_target_edge_ids
