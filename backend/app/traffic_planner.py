"""Coverage-first waypoint chain and single-flight observation collection."""
import time
from collections import Counter
from . import schemas as s
from .traffic_runtime import run_bounded
from .traffic_cache import OBSERVATION_CACHE
from .traffic_geometry import correct_segment, distance, project


def _bbox(geometry):
    return min(p[0] for p in geometry),min(p[1] for p in geometry),max(p[0] for p in geometry),max(p[1] for p in geometry)


def _near(a,b):
    return a[0]<=b[2]+.001 and a[2]>=b[0]-.001 and a[1]<=b[3]+.001 and a[3]>=b[1]-.001


def candidate_segments(routes, graph, deadline=None, clock=time.monotonic):
    result=[]
    if graph is None: return result
    for route in routes:
        for leg in route.legs:
            if leg.line and leg.mode in ('bus','trolleybus','tram'):
                try:
                    def read():
                        return graph.trip_segments(leg.line.trip_id,service_day=None,
                            from_stop_id=leg.origin.stop_id,to_stop_id=leg.destination.stop_id)
                    result.extend(run_bounded(read,deadline-.05,clock) if deadline is not None else read())
                except (ValueError,KeyError,TimeoutError):
                    continue
    return result


def plan_waypoints(segments, max_points=50):
    """Join ordered target edges; connectors never become target edges.

    Shared edges go first. Only target endpoints are anchors, reducing long
    common corridors without filling the quota with purposeless stops.
    """
    counts=Counter(x.edge_key for x in segments)
    unique={x.edge_key:x for x in segments if x.geometry and len(x.geometry)>1}
    outgoing={}; incoming={}
    for key,x in unique.items():
        outgoing.setdefault((x.feed_id,x.from_stop_id),[]).append(key)
        incoming.setdefault((x.feed_id,x.to_stop_id),[]).append(key)
    chains=[]; seen=set()
    starts=sorted(unique,key=lambda key:(len(incoming.get((key[0],key[1]),[]))==1,key))
    for key in starts:
        if key in seen: continue
        chain=[]; current=key
        while current not in seen:
            seen.add(current); x=unique[current]; chain.append(x)
            end=(x.feed_id,x.to_stop_id)
            next_keys=outgoing.get(end,[])
            if len(next_keys)!=1 or len(incoming.get(end,[]))!=1: break
            nxt=next_keys[0]
            if counts[nxt]!=counts[current]: break
            current=nxt
        chains.append(chain)
    points=[]; targets=[]; connectors=[]
    while chains and len(points)<max_points:
        chains.sort(key=lambda chain:(-max(counts[x.edge_key] for x in chain),
            distance(points[-1],chain[0].geometry[0]) if points else 0,chain[0].edge_key))
        chain=chains.pop(0)
        anchors=[chain[0].geometry[0]]+[x.geometry[-1] for x in chain]
        # Preserve bends and long corridors before expanding into neighbors.
        # Allocate fairly across outstanding chains rather than exhausting the
        # quota on the first candidate's stops.
        allocation=max(2,(max_points-len(points))//(len(chains)+1))
        selected={0,len(anchors)-1}
        intervals=[(0,len(anchors)-1)]
        while intervals and len(selected)<allocation:
            lo,hi=intervals.pop(0)
            if hi-lo<=1: continue
            errors=[(project(anchors[i],anchors[lo],anchors[hi])[1],i) for i in range(lo+1,hi)]
            error,index=max(errors)
            if error<=20 and distance(anchors[lo],anchors[hi])<=500: continue
            if error<=20: index=(lo+hi)//2
            selected.add(index); intervals.extend([(lo,index),(index,hi)])
        chosen=[anchors[i] for i in sorted(selected)]
        start=chosen[0]
        required=len(chosen)-(1 if points and points[-1]==start else 0)
        if len(points)+required>max_points: continue
        if points and points[-1]!=start: connectors.append((points[-1],start))
        if points and points[-1]==start: chosen=chosen[1:]
        points.extend(chosen); targets.extend(x.edge_key for x in chain)
    return points,targets,connectors


class ObservationCollector:
    def __init__(self, roads, dgis_roads, *, cache=OBSERVATION_CACHE, clock=time.monotonic, wall_clock=time.time):
        self.roads,self.dgis_roads,self.cache=roads,dgis_roads,cache
        self.clock,self.wall_clock=clock,wall_clock

    def __call__(self, routes, graph, deadline, request=None):
        # Reading all candidate trip variants first lets the graph mark shared
        # stop-pair geometries ambiguous before any paid collection decision.
        candidate_segments(routes,graph,deadline,self.clock)
        segments=candidate_segments(routes,graph,deadline,self.clock)
        boxes=[_bbox(x.geometry) for x in segments if len(x.geometry)>1]
        observations=[]
        for values in self.cache.values():
            for observation in values:
                if self.clock()>=deadline-.05: break
                if observation.fresh(self.wall_clock()):
                    box=_bbox(observation.geometry)
                    if any(_near(box,candidate) for candidate in boxes): observations.append(observation)
        calls=[]
        def uncovered():
            return [x for x in segments if (r:=correct_segment(x,observations,now=self.wall_clock(),deadline=deadline,clock=self.clock)).covered_m<r.applicable_m-.01]
        for name,provider,purpose in [('2gis',self.dgis_roads,'car_alternatives'),('yandex',self.roads,'traffic_collection')]:
            missing=uncovered()
            if not missing:
                calls.append(s.ProviderCallDebug(provider=name,purpose=purpose,status='cache_hit',billable_units=0,
                    skip_reason='All applicable candidate edges have fresh usable observations'))
                continue
            if self.clock()>=deadline-.05 or provider is None:
                calls.append(s.ProviderCallDebug(provider=name,purpose=purpose,status='skipped',billable_units=0,
                    skip_reason='deadline' if self.clock()>=deadline-.05 else 'provider_unavailable'))
                continue
            targets=[]; connectors=[]
            if name=='2gis':
                # One A/B calculation, alternatives all returned by the provider.
                best=max(missing,key=lambda x:len(x.geometry))
                points=[best.geometry[0],best.geometry[-1]]
                # Prefer the first/last applicable stops of a complete candidate.
                selected=candidate_segments(routes[:1],graph,deadline,self.clock)
                if selected and selected[0].geometry and selected[-1].geometry:
                    points=[selected[0].geometry[0],selected[-1].geometry[-1]]
                if request is not None:
                    points=[(p.lon,p.lat) for p in request.points]
            else:
                # Candidate anchors are assigned before neighborhood expansion.
                points,targets,connectors=plan_waypoints(missing)
                if getattr(graph,'topology_ready',False) and len(points)<50 and self.clock()<deadline-.1:
                    neighbors=graph.candidate_edge_neighbors([x.edge_key for x in segments],depth=2)
                    pending=list(neighbors)
                    used={x.edge_key for x in segments}
                    prepared={}
                    while pending and len(points)<49 and self.clock()<deadline-.1:
                        priority=min((edge['depth'],-edge.get('line_count',0)) for edge in pending)
                        layer=[edge for edge in pending if (edge['depth'],-edge.get('line_count',0))==priority]
                        choices=[]
                        for edge in layer:
                            if self.clock()>=deadline-.1: break
                            key=edge['key']
                            if key not in prepared:
                                try: prepared[key]=run_bounded(lambda:graph.edge_segment(edge),deadline-.05,self.clock)
                                except (KeyError,TypeError,ValueError,TimeoutError): prepared[key]=None
                            segment=prepared[key]
                            if segment is None or not segment.geometry or segment.edge_key in used: continue
                            attachment=distance(points[-1],segment.geometry[0]) if points else 0
                            choices.append((attachment,str(key),edge,segment))
                        if not choices:
                            pending=[edge for edge in pending if edge not in layer]
                            continue
                        _,_,edge,segment=min(choices,key=lambda item:item[:2])
                        pending.remove(edge)
                        extra,_,_=plan_waypoints([segment],max_points=50-len(points))
                        if extra:
                            if points and points[-1]!=extra[0]: connectors.append((points[-1],extra[0]))
                            if points and points[-1]==extra[0]: extra=extra[1:]
                            points.extend(extra); used.add(segment.edge_key);targets.append(segment.edge_key)
            if len(points)<2:
                calls.append(s.ProviderCallDebug(provider=name,purpose=purpose,status='skipped',billable_units=0,skip_reason='no_usable_waypoints'))
                continue
            metadata=dict(waypoint_coordinates=points,
                target_edge_ids=[':'.join(k) for k in targets],
                unrepresented_target_edge_ids=sorted({':'.join(x.edge_key) for x in missing}-set(':'.join(k) for k in targets)) if name=='yandex' else [],
                connectors=[s.LineString(coordinates=list(pair)) for pair in connectors],
                planned_chain_chord_meters=sum(distance(a,b) for a,b in zip(points,points[1:])))
            key=(name,tuple(tuple(p) for p in points))
            hit=self.cache.get(key) is not None
            started=self.clock(); paid=[False]
            def load():
                if self.clock()>=deadline-.05: raise TimeoutError('search_deadline')
                paid[0]=True
                return provider.estimate_observations(points,timeout_seconds=max(.001,deadline-self.clock()-.05))
            try:
                received=run_bounded(lambda:self.cache.get_or_load(key,load,timeout=max(.001,deadline-self.clock()-.05)),deadline-.05,self.clock)
                observations.extend(received)
                calls.append(s.ProviderCallDebug(provider=name,purpose=purpose,
                    status='cache_hit' if hit else 'succeeded' if paid[0] else 'coalesced',
                    billable_units=int(paid[0]),waypoints_count=len(points),
                    alternatives_received=len({o.alternative_index for o in received}),
                    actual_chain_distance_meters=sum(o.length_m for o in received if o.alternative_index==0),
                    actual_chain_duration_seconds=sum(o.duration_seconds for o in received if o.alternative_index==0),
                    **metadata,
                    elapsed_milliseconds=max(0,(self.clock()-started)*1000)))
            except Exception as exc:
                calls.append(s.ProviderCallDebug(provider=name,purpose=purpose,status='failed',billable_units=int(paid[0]),
                    waypoints_count=len(points),error_code=getattr(exc,'code',type(exc).__name__),**metadata,
                    elapsed_milliseconds=max(0,(self.clock()-started)*1000)))
        unique_segments={x.edge_key:x for x in segments}
        useful=sum(correct_segment(x,observations,now=self.wall_clock(),deadline=deadline,clock=self.clock).covered_m for x in unique_segments.values())
        for call in calls:
            if call.purpose=='traffic_collection': call.useful_covered_distance_meters=useful
        return list({o.id:o for o in observations}.values()),calls
