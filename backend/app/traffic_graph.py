"""Trip-local reads of the existing disk index, pinned to active OTP sources."""
from collections import OrderedDict
from contextlib import closing, nullcontext
import json
import re
from pathlib import Path
import threading
import sqlite3
from .gtfs_index import FORMAT,connect,index_directory,DiskSnapshot,valid
from .gtfs_store import seconds,route_mode
from .traffic_cache import _memory_size
from .traffic_data import SegmentBaseline
from .traffic_geometry import distance,length,project,slice_line

class TrafficGraph:
    def __init__(self,snapshot,feed_id='gtfs',graph_version=None):
        self.snapshot=snapshot; self.feed_id=feed_id; self.version=getattr(snapshot,'version',None)
        self.graph_version=graph_version; self._trips=OrderedDict(); self._lock=threading.RLock(); self.edges={}; self.adjacency={}; self.topology_ready=False; self.road_statuses={}; self.road_status_metadata={}; self.road_status_version=None; self._edge_geometries=OrderedDict(); self._ambiguous_edges=set(); self.max_trip_bytes=24*1024*1024; self.max_evidence_bytes=8*1024*1024; self._trip_sizes={}; self._trip_bytes=0; self._evidence_sizes={}; self._evidence_bytes=0

    @classmethod
    def from_snapshot(cls,snapshot,feed_id='gtfs',graph_version=None):
        return cls(snapshot,feed_id,graph_version)

    def load_road_statuses(self,path):
        """Optional curated directed-edge evidence; absence remains unknown.

        No official text list is automatically treated as a ready geometry layer.
        Every positive entry requires provenance and a version date.
        """
        data=json.loads(Path(path).read_text())
        if not data.get('version'): raise ValueError('Road status version missing')
        statuses={}; metadata={}
        pins=data.get('feed_sha256',{})
        if self.feed_id in pins and pins[self.feed_id]!=self.version: raise ValueError('Road status feed identity mismatch')
        import math
        for row in data['edges']:
            fingerprints=set()
            for variant in row.get('variants',[]):
                fingerprint=variant.get('geometry_sha256')
                if not isinstance(fingerprint,str) or re.fullmatch('[a-f0-9]{64}',fingerprint) is None or fingerprint in fingerprints:
                    raise ValueError('Invalid or duplicate geometry variant')
                fingerprints.add(fingerprint)
                if not variant.get('source') or not variant.get('date'): raise ValueError('Variant provenance missing')
                from datetime import datetime
                datetime.fromisoformat(variant['date'])
                previous=0
                for span in variant['spans']:
                    a,b=span['start_m'],span['end_m']
                    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (a,b)) or a<previous or b<=a:
                        raise ValueError('Invalid variant lane spans')
                    if span.get('status') not in ('dedicated','segregated','shared','unknown'): raise ValueError('Invalid variant status')
                    if not span.get('source',variant['source']): raise ValueError('Variant span provenance missing')
                    datetime.fromisoformat(span.get('date',variant['date']))
                    previous=b
            if not row.get('source') or not row.get('date'): raise ValueError('Road status provenance missing')
            if row['status'] not in ('dedicated','segregated','shared','unknown'): raise ValueError('Invalid road status')
            key=(row['feed_id'],row['from_stop_id'],row['to_stop_id'])
            if key in statuses: raise ValueError('Duplicate road status edge')
            if pins and not row.get('geometry_sha256'): raise ValueError('Road status geometry identity missing')
            statuses[key]=row['status']
            from datetime import datetime, timezone
            observed=datetime.fromisoformat(row['date'])
            if observed.tzinfo is None: observed=observed.replace(tzinfo=timezone.utc)
            spans=[]
            for span in sorted(row.get('spans',[]),key=lambda s:s['start_m']):
                start,end=span['start_m'],span['end_m']
                if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end)) or start<0 or end<=start:
                    raise ValueError('Invalid road status span')
                if spans and start<spans[-1]['end_m']: raise ValueError('Overlapping road status spans')
                status=span.get('status',row['status'])
                if status not in ('dedicated','segregated','shared','unknown'): raise ValueError('Invalid road status span')
                span_date=datetime.fromisoformat(span.get('date',row['date']))
                if span_date.tzinfo is None: span_date=span_date.replace(tzinfo=timezone.utc)
                spans.append({'start_m':start,'end_m':end,'status':status,'source':span.get('source',row['source']),'observed_at':span_date,'version':str(data['version'])})
            metadata[(row['feed_id'],row['from_stop_id'],row['to_stop_id'])]={'lane_source_url':row['source'],'lane_source_observed_at':observed,'road_status_version':str(data['version']),'lane_spans':tuple(spans),'_geometry_sha256':row.get('geometry_sha256'),'_variants':row.get('variants')}
            # A profile containing spans describes only those spans; unspecified
            # road remains unknown regardless of the span's inherited status.
            if spans: statuses[(row['feed_id'],row['from_stop_id'],row['to_stop_id'])]='unknown'

        with self._lock:
            self.road_statuses=statuses; self.road_status_metadata=metadata; self.road_status_version=data['version']; self._trips.clear(); self._trip_sizes.clear(); self._trip_bytes=0

    def warm_topology(self):
        """Explicit startup warmup; never invoked by a user search."""
        disk=getattr(self.snapshot,'disk',None)
        if disk is None: return False
        sql="""WITH ordered AS (
            SELECT trip_id,stop_id,CAST(stop_sequence AS INTEGER) AS sequence,
                   LEAD(stop_id) OVER (PARTITION BY trip_id ORDER BY CAST(stop_sequence AS INTEGER)) AS next_stop
            FROM gtfs_stop_times)
            SELECT o.stop_id,o.next_stop,t.route_id,MIN(o.trip_id),MIN(o.sequence)
            FROM ordered o JOIN gtfs_trips t ON t.trip_id=o.trip_id
            WHERE o.next_stop IS NOT NULL GROUP BY o.stop_id,o.next_stop,t.route_id"""
        edges={}; adjacency={}
        path=getattr(self.snapshot,'path',None)
        connection=closing(connect(path)) if path is not None else nullcontext(disk.db)
        with connection as warm_db:
            for a,b,route,trip,sequence in warm_db.execute(sql):
                key=(self.feed_id,a,b)
                edge=edges.setdefault(key,{'key':key,'lines':set(),'trip_id':trip,'from_sequence':sequence})
                edge['lines'].add(route)
        for key,edge in edges.items():
            edge['line_count']=len(edge['lines'])
            adjacency.setdefault(key[1],[]).append(edge)
        with self._lock:
            self.edges=edges; self.adjacency=adjacency; self.topology_ready=True
        return True

    def candidate_edge_neighbors(self,edge_keys,depth=2):
        seen=set(edge_keys); frontier={k[2] for k in edge_keys}; result=[]
        for layer in range(1,min(depth,2)+1):
            following=set()
            for stop in sorted(frontier):
                for edge in sorted(self.adjacency.get(stop,[]),key=lambda e:(-e['line_count'],e['key'])):
                    if edge['key'] in seen: continue
                    seen.add(edge['key']); following.add(edge['key'][2])
                    result.append(dict(edge,depth=layer))
            frontier=following
        return result

    def edge_segment(self,edge):
        for segment in self.trip_segments(edge['trip_id']):
            if segment.edge_key==edge['key']:
                # A stop-pair representative has no caller-selected trip shape.
                # Its auxiliary ambiguity must never contaminate exact trips.
                if segment.edge_key in self._ambiguous_edges:
                    from dataclasses import replace
                    return replace(segment,ambiguous=True)
                return segment
        return None

    def _native_id(self,value,table,field):
        if value is None: return None
        if next(self.snapshot.rows(table,required=False,filters={field:value}),None) is not None: return value
        return value.split(':',1)[1] if ':' in value else value

    def transfer_minimum(self,from_stop_id,to_stop_id,from_trip_id=None,to_trip_id=None):
        from_stop_id=self._native_id(from_stop_id,'stops.txt','stop_id')
        to_stop_id=self._native_id(to_stop_id,'stops.txt','stop_id')
        from_trip_id=self._native_id(from_trip_id,'trips.txt','trip_id')
        to_trip_id=self._native_id(to_trip_id,'trips.txt','trip_id')
        def route(trip_id):
            if trip_id is None: return None
            return next(self.snapshot.rows('trips.txt',filters={'trip_id':trip_id}),{}).get('route_id')
        fields={'from_stop_id':from_stop_id,'to_stop_id':to_stop_id,'from_trip_id':from_trip_id,'to_trip_id':to_trip_id,'from_route_id':route(from_trip_id),'to_route_id':route(to_trip_id)}
        # Platform-specific rules override parent-station rules.
        parents={}
        for name,stop_id in [('from_stop_id',from_stop_id),('to_stop_id',to_stop_id)]:
            parents[name]=next(self.snapshot.rows('stops.txt',filters={'stop_id':stop_id}),{}).get('parent_station')
        matches=[]
        for row in self.snapshot.rows('transfers.txt',required=False):
            score=0; compatible=True
            for name,value in fields.items():
                rule=row.get(name)
                if not rule: continue
                if rule==value: score+=100 if 'trip' in name else (10 if 'route' in name else 2)
                elif name in parents and rule==parents[name]: score+=1
                else: compatible=False; break
            if compatible: matches.append((score,row))
        if not matches: return {'minimum_seconds':None,'forbidden':False,'source':'none'}
        score=max(m[0] for m in matches); selected=[row for priority,row in matches if priority==score]
        forbidden=any(row.get('transfer_type')=='3' for row in selected)
        minimum=max((float(row['min_transfer_time']) for row in selected if row.get('transfer_type')=='2' and row.get('min_transfer_time')),default=None)
        return {'minimum_seconds':minimum,'forbidden':forbidden,'source':'gtfs'}

    def service_info(self,trip_id,service_day=None,departure_at=None,boarding_stop_id=None,boarding_sequence=None):
        trip_id=self._native_id(trip_id,'trips.txt','trip_id')
        if next(self.snapshot.rows('trips.txt',required=False,filters={'trip_id':trip_id}),None) is None:
            return {'service_kind':'unknown','frequency_exact_times':None,'headway_seconds':None}
        frequencies=list(self.snapshot.rows('frequencies.txt',required=False,filters={'trip_id':trip_id}))
        if not frequencies: return {'service_kind':'scheduled','frequency_exact_times':None,'headway_seconds':None}
        if departure_at is not None:
            if hasattr(departure_at,'astimezone') and departure_at.tzinfo is not None:
                from zoneinfo import ZoneInfo
                agency=next(self.snapshot.rows('agency.txt',required=False),{})
                timezone_name=agency.get('agency_timezone')
                if timezone_name: departure_at=departure_at.astimezone(ZoneInfo(timezone_name))
            offset=departure_at.hour*3600+departure_at.minute*60+departure_at.second if hasattr(departure_at,'hour') else seconds(str(departure_at))
            if service_day is not None and hasattr(departure_at,'date'):
                from datetime import date
                day=date.fromisoformat(str(service_day))
                offset+=(departure_at.date()-day).days*86400
            if boarding_stop_id is not None or boarding_sequence is not None:
                rows=sorted(self.snapshot.rows('stop_times.txt',filters={'trip_id':trip_id}),key=lambda r:int(r['stop_sequence']))
                stop=self._native_id(boarding_stop_id,'stops.txt','stop_id')
                matches=[r for r in rows if (stop is None or r['stop_id']==stop) and (boarding_sequence is None or int(r['stop_sequence'])==boarding_sequence)]
                if rows and len(matches)==1:
                    try: offset-=seconds(matches[0]['departure_time'])-seconds(rows[0]['departure_time'])
                    except (KeyError,ValueError,TypeError): pass
            selected=[r for r in frequencies if seconds(r['start_time'])<=offset<seconds(r['end_time'])]
            if selected: frequencies=selected
            else: return {'service_kind':'frequency','frequency_exact_times':0,'headway_seconds':min(int(r['headway_secs']) for r in frequencies)}
        exact={int(r.get('exact_times') or 0) for r in frequencies}
        return {'service_kind':'frequency','frequency_exact_times':next(iter(exact)) if len(exact)==1 else 0,'headway_seconds':min(int(r['headway_secs']) for r in frequencies)}

    def trip_segments(self,trip_id,service_day=None,from_stop_id=None,to_stop_id=None,from_sequence=None,to_sequence=None):
        trip_id=self._native_id(trip_id,'trips.txt','trip_id')
        from_stop_id=self._native_id(from_stop_id,'stops.txt','stop_id')
        to_stop_id=self._native_id(to_stop_id,'stops.txt','stop_id')
        with self._lock:
            if trip_id not in self._trips:
                value=self._read_trip(trip_id)
                size=_memory_size(value)
                if size<=self.max_trip_bytes:
                    self._trips[trip_id]=value; self._trip_sizes[trip_id]=size; self._trip_bytes+=size
                    while len(self._trips)>512 or self._trip_bytes>self.max_trip_bytes:
                        removed,_=self._trips.popitem(last=False); self._trip_bytes-=self._trip_sizes.pop(removed)
                base=value
            else:
                base=self._trips[trip_id]; self._trips.move_to_end(trip_id)
        start=0; end=len(base)
        if from_stop_id is not None:
            candidates=[i for i,s in enumerate(base) if s.from_stop_id==from_stop_id and (from_sequence is None or s.from_sequence==from_sequence)]
            if len(candidates)!=1: return []
            start=candidates[0]
        if to_stop_id is not None:
            candidates=[i+1 for i,s in enumerate(base) if i>=start and s.to_stop_id==to_stop_id and (to_sequence is None or s.to_sequence==to_sequence)]
            if len(candidates)!=1: return []
            end=candidates[0]
        from dataclasses import replace
        return [replace(s,service_day=str(service_day) if service_day else None,dwell_seconds=s.dwell_seconds if i<end-1 else 0) for i,s in enumerate(base) if start<=i<end]

    def _read_trip(self,trip_id):
        trips=list(self.snapshot.rows('trips.txt',filters={'trip_id':trip_id}))
        if not trips: return []
        trip=trips[0]
        rows=sorted(self.snapshot.rows('stop_times.txt',filters={'trip_id':trip_id}),key=lambda r:int(r['stop_sequence']))
        shape=sorted(self.snapshot.rows('shapes.txt',required=False,filters={'shape_id':trip.get('shape_id','')}),key=lambda r:int(r['shape_pt_sequence'])) if trip.get('shape_id') else []
        geometry=tuple((float(r['shape_pt_lon']),float(r['shape_pt_lat'])) for r in shape)
        shape_dist=[float(r['shape_dist_traveled']) if r.get('shape_dist_traveled') else None for r in shape]
        routes=list(self.snapshot.rows('routes.txt',filters={'route_id':trip['route_id']}))
        mode=route_mode(routes[0]['route_type']) if routes else 'unknown'
        invalid_shape_dist=any(a is not None and b is not None and b<a for a,b in zip(shape_dist,shape_dist[1:]))
        if invalid_shape_dist: geometry=()
        offsets=[0]
        for a,b in zip(geometry,geometry[1:]): offsets.append(offsets[-1]+distance(a,b))
        def position(row,minimum):
            if row.get('shape_dist_traveled') and shape_dist and all(d is not None for d in shape_dist):
                target=float(row['shape_dist_traveled'])
                for i,(a,b) in enumerate(zip(shape_dist,shape_dist[1:])):
                    if a<=target<=b and b>a:
                        value=offsets[i]+(target-a)/(b-a)*(offsets[i+1]-offsets[i])
                        return value if value>=minimum-0.01 else None
            stops=list(self.snapshot.rows('stops.txt',filters={'stop_id':row['stop_id']}))
            if not stops: return None
            stop=stops[0]; p=(float(stop['stop_lon']),float(stop['stop_lat']))
            candidates=[]
            for i,(a,b) in enumerate(zip(geometry,geometry[1:])):
                t,error=project(p,a,b); at=offsets[i]+t*(offsets[i+1]-offsets[i])
                if at>=minimum-0.01: candidates.append((error,at))
            if not candidates: return None
            error,at=min(candidates)
            return at if error<=50 else None
        positions=[]; previous=0
        for row in rows:
            at=position(row,previous) if geometry else None; positions.append(at)
            if at is not None: previous=at
        result=[]
        for i,(a,b) in enumerate(zip(rows,rows[1:])):
            try: moving=seconds(b['arrival_time'])-seconds(a['departure_time'])
            except (KeyError,ValueError,TypeError): moving=None
            if moving is not None and moving<0: moving=None
            try: dwell=max(0,seconds(b['departure_time'])-seconds(b['arrival_time']))
            except (KeyError,ValueError,TypeError): dwell=0
            line=slice_line(geometry,positions[i],positions[i+1]) if positions[i] is not None and positions[i+1] is not None else ()
            status=self.road_statuses.get((self.feed_id,a['stop_id'],b['stop_id']),'unknown')
            key=(self.feed_id,a['stop_id'],b['stop_id'])
            evidence=self._edge_geometries.get(key)
            if evidence is None:
                evidence={'geometry':line,'ambiguous':key in self._ambiguous_edges}
                size=_memory_size(evidence)+_memory_size(key)
                if size<=self.max_evidence_bytes:
                    self._edge_geometries[key]=evidence; self._evidence_sizes[key]=size; self._evidence_bytes+=size
                    while self._evidence_bytes>self.max_evidence_bytes:
                        removed,_=self._edge_geometries.popitem(last=False); self._evidence_bytes-=self._evidence_sizes.pop(removed)
            elif key in self._edge_geometries: self._edge_geometries.move_to_end(key)
            if line and evidence['geometry'] and line != evidence['geometry']:
                from .traffic_geometry import oriented_overlap
                old=evidence['geometry']
                # Bound compatibility work independently of raw shape density.
                # Identical paths were handled above in linear tuple equality.
                def sampled(points):
                    if len(points)<=64: return points
                    return tuple(points[round(i*(len(points)-1)/63)] for i in range(64))
                current_sample=sampled(line); old_sample=sampled(old)
                covered=sum(m[1]-m[0] for m in oriented_overlap(current_sample,old_sample,max_distance=5,max_angle=15))
                if covered<length(current_sample)*.95 or abs(length(line)-length(old))>max(10,length(old)*.05):
                    evidence['ambiguous']=True; self._ambiguous_edges.add(key)
            metadata=dict(self.road_status_metadata.get(key,{}))
            expected=metadata.pop('_geometry_sha256',None)
            variants=metadata.pop('_variants',None)
            if variants:
                from .traffic_lane_builder import geometry_fingerprint
                from datetime import datetime,timezone
                selected=[v for v in variants if v.get('geometry_sha256')==geometry_fingerprint(line)]
                if len(selected)!=1: metadata={}; status='unknown'; expected=None
                else:
                    variant=selected[0]; expected=variant['geometry_sha256']
                    def observed(value):
                        dt=datetime.fromisoformat(value)
                        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                    metadata={'lane_source_url':variant['source'],'lane_source_observed_at':observed(variant['date']),'road_status_version':self.road_status_version,'lane_spans':tuple(dict(span,source=span.get('source',variant['source']),observed_at=observed(span.get('date',variant['date'])),version=self.road_status_version) for span in variant['spans'])}
            if expected:
                from .traffic_lane_builder import geometry_fingerprint
                if geometry_fingerprint(line)!=expected: metadata={}; status='unknown'
            result.append(SegmentBaseline(self.feed_id,self.version,trip_id,a['stop_id'],b['stop_id'],int(a['stop_sequence']),int(b['stop_sequence']),line,moving,dwell,trip.get('service_id'),ambiguous=invalid_shape_dist,mode=mode,road_status=status,**metadata))
        return result

class _PinnedSnapshot:
    def __init__(self,path,version):
        self.path=path; self.version=version
        self.db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA cache_size=-8192')
        self.disk=DiskSnapshot(self.db); self.lock=threading.RLock()
    def rows(self,name,required=True,filters=None):
        with self.lock:
            yield from self.disk.rows(name,required,filters)
    def __del__(self):
        if hasattr(self,'db'): self.db.close()

_GRAPHS=OrderedDict(); _LOCK=threading.RLock()

def graph_for_active(root,gtfs_version=None,graph_version=None):
    """Never build an index or silently substitute latest for active graph data."""
    root=Path(root)
    try:
        active=json.loads((root/'otp'/'current.json').read_text())
        version=active['gtfs_source_sha256']
        if not isinstance(version,str) or re.fullmatch('[a-f0-9]{64}',version) is None: return None
        active_graph=active.get('graph_version') or active.get('version') or active.get('sha256')
        if gtfs_version is not None and gtfs_version!=version: return None
        if graph_version is not None and active_graph is not None and graph_version!=active_graph: return None
        path=index_directory()/f'v{FORMAT}-{version}.sqlite3'
        status_path=root/'traffic-road-status.json'
        if not status_path.exists(): status_path=Path(__file__).parent/'fixtures'/'traffic'/'road-status.json'
        profile_signature=(str(status_path),status_path.stat().st_mtime_ns,status_path.stat().st_size) if status_path.exists() else None
        key=(str(path),version,graph_version,profile_signature)
        with _LOCK:
            if key not in _GRAPHS:
                if not valid(path,version): return None
                _GRAPHS[key]=TrafficGraph(_PinnedSnapshot(path,version),graph_version=graph_version)
                graphs={'gtfs':_GRAPHS[key]}
                for feed in ('metro','rail'):
                    source=active.get(feed+'_source_sha256')
                    if not isinstance(source,str) or re.fullmatch('[a-f0-9]{64}',source) is None: continue
                    feed_path=index_directory()/f'v{FORMAT}-{source}.sqlite3'
                    if valid(feed_path,source): graphs[feed]=TrafficGraph(_PinnedSnapshot(feed_path,source),feed_id=feed,graph_version=graph_version)
                if len(graphs)>1: _GRAPHS[key]=CompositeTrafficGraph(graphs)
                if status_path.exists():
                    try: _GRAPHS[key].load_road_statuses(status_path)
                    except (OSError,ValueError,KeyError,TypeError): pass
            _GRAPHS.move_to_end(key)
            while len(_GRAPHS)>2: _GRAPHS.popitem(last=False)
            return _GRAPHS[key]
    except (OSError,ValueError,KeyError,TypeError): return None

class CompositeTrafficGraph:
    """Resolve IDs against active feeds without guessing colliding namespaces."""
    def __init__(self,graphs):
        self.graphs=graphs; self.surface=graphs['gtfs']
    def __getattr__(self,name): return getattr(self.surface,name)
    def _select(self,trip_id,feed_id=None):
        graphs={feed_id:self.graphs[feed_id]} if feed_id in self.graphs else self.graphs
        # A native ID may itself contain colons. Check its complete form first.
        for candidate in (trip_id,trip_id.split(':',1)[1] if ':' in trip_id else trip_id):
            matches=[(g,candidate) for g in graphs.values() if next(g.snapshot.rows('trips.txt',required=False,filters={'trip_id':candidate}),None) is not None]
            if len(matches)==1: return matches[0]
            if len(matches)>1: return None,None
        return None,None
    def service_info(self,trip_id,service_day=None,departure_at=None,boarding_stop_id=None,boarding_sequence=None,feed_id=None):
        graph,native=self._select(trip_id,feed_id)
        if graph is None: return {'service_kind':'unknown','frequency_exact_times':None,'headway_seconds':None}
        return graph.service_info(native,service_day,departure_at,boarding_stop_id,boarding_sequence)
    def trip_segments(self,trip_id,service_day=None,from_stop_id=None,to_stop_id=None,from_sequence=None,to_sequence=None,feed_id=None):
        graph,native=self._select(trip_id,feed_id)
        if graph is None: return []
        return graph.trip_segments(native,service_day,from_stop_id,to_stop_id,from_sequence,to_sequence)
    def transfer_minimum(self,from_stop_id,to_stop_id,from_trip_id=None,to_trip_id=None):
        matches=[]
        for graph in self.graphs.values():
            result=graph.transfer_minimum(from_stop_id,to_stop_id,from_trip_id,to_trip_id)
            if result['source']!='none': matches.append(result)
        if not matches: return {'minimum_seconds':None,'forbidden':False,'source':'none'}
        return {'minimum_seconds':max((r['minimum_seconds'] for r in matches if r['minimum_seconds'] is not None),default=None),'forbidden':any(r['forbidden'] for r in matches),'source':'gtfs'}
