"""Conservative oriented overlap and positive, non-overlapping corrections."""
import math
import time
from .traffic_data import CorrectionResult

EARTH = 6371000

def xy(point, latitude):
    return math.radians(point[0])*EARTH*math.cos(math.radians(latitude)), math.radians(point[1])*EARTH

def distance(a,b):
    x,y=xy(a,(a[1]+b[1])/2); u,v=xy(b,(a[1]+b[1])/2)
    return math.hypot(u-x,v-y)

def length(geometry):
    return sum(distance(a,b) for a,b in zip(geometry,geometry[1:]))

def project(point,a,b):
    latitude=(a[1]+b[1])/2
    p=xy(point,latitude); x=xy(a,latitude); y=xy(b,latitude)
    dx,dy=y[0]-x[0],y[1]-x[1]
    d=dx*dx+dy*dy
    t=((p[0]-x[0])*dx+(p[1]-x[1])*dy)/d if d else 0
    t=max(0,min(1,t))
    return t, math.hypot(p[0]-x[0]-t*dx,p[1]-x[1]-t*dy)

def slice_line(geometry,start,end):
    """Slice a directed line by metric offsets, preserving intermediate bends."""
    if end<=start or len(geometry)<2: return ()
    result=[]; offset=0
    for a,b in zip(geometry,geometry[1:]):
        size=distance(a,b)
        if size and offset+size>start and offset<end:
            lo=max(0,(start-offset)/size); hi=min(1,(end-offset)/size)
            for t in (lo,hi):
                p=(a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1]))
                if not result or result[-1]!=p: result.append(p)
        offset+=size
    return tuple(result)

def oriented_overlap(bus,road,max_distance=20,max_angle=30,deadline=None,clock=time.monotonic):
    """Return bus intervals with road metric bounds and mean lateral error.

    Endpoints are projected monotonically; opposite lanes and single crossings
    cannot produce a substantial aligned interval.
    """
    matches=[]; bo=0; road_offsets=[]; ro=0
    for c,d in zip(road,road[1:]):
        road_offsets.append((c,d,ro,distance(c,d))); ro+=distance(c,d)
    for a,b in zip(bus,bus[1:]):
        if deadline is not None and clock()>=deadline: break
        bl=distance(a,b)
        if not bl: continue
        candidates=[]
        lat=(a[1]+b[1])/2; ax,ay=xy(a,lat); bx,by=xy(b,lat)
        for c,d,offset,rl in road_offsets:
            if deadline is not None and clock()>=deadline: break
            padding=max_distance/111000
            lon_padding=padding/max(0.01,math.cos(math.radians(lat)))
            if max(a[0],b[0])+lon_padding<min(c[0],d[0]) or max(c[0],d[0])+lon_padding<min(a[0],b[0]) or max(a[1],b[1])+padding<min(c[1],d[1]) or max(c[1],d[1])+padding<min(a[1],b[1]): continue
            if not rl: continue
            cx,cy=xy(c,lat); dx,dy=xy(d,lat)
            cosine=((bx-ax)*(dx-cx)+(by-ay)*(dy-cy))/(math.hypot(bx-ax,by-ay)*math.hypot(dx-cx,dy-cy))
            if cosine < math.cos(math.radians(max_angle)): continue
            t1,e1=project(c,a,b); t2,e2=project(d,a,b)
            # Check overlap endpoints against the road, rather than accepting
            # clamped endpoints far beyond its actual covered extent.
            if t2<=t1: continue
            p=(a[0]+t1*(b[0]-a[0]),a[1]+t1*(b[1]-a[1])); q=(a[0]+t2*(b[0]-a[0]),a[1]+t2*(b[1]-a[1]))
            u,ep=project(p,c,d); v,eq=project(q,c,d)
            if max(ep,eq)>max_distance or v<=u: continue
            candidates.append((bo+t1*bl,bo+t2*bl,offset+u*rl,offset+v*rl,(ep+eq)/2))
        matches.extend(candidates); bo+=bl
    # Reject repeated road occurrences with equally plausible metric projections.
    for i,m in enumerate(matches):
        if deadline is not None and clock()>=deadline: return []
        for n in matches[i+1:]:
            if min(m[1],n[1])-max(m[0],n[0])>1 and abs(m[4]-n[4])<1 and abs(m[2]-n[2])>20:
                return []
    ordered=sorted(matches,key=lambda m:(m[0],m[1],m[2]))
    last_bus=-1; last_road=-1
    for match in ordered:
        if match[0]>=last_bus-0.01 and match[2]<last_road-1: return []
        last_bus=max(last_bus,match[1]); last_road=max(last_road,match[3])
    return matches

def correct_segment(segment,observations,now=None,max_distance=20,max_angle=30,deadline=None,clock=time.monotonic):
    now=time.time() if now is None else now
    total=length(segment.geometry); result=CorrectionResult(applicable_m=total)
    if segment.mode in ('metro','rail') or segment.road_status in ('dedicated','segregated'):
        result.applicable_m=0; return result
    spans=sorted(getattr(segment,'lane_spans',()),key=lambda s:s['start_m'])
    excluded=[]; previous_end=-1
    for span in spans:
        start,end=span['start_m'],span['end_m']
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) or start<0 or end<=start or start<previous_end or end>total+0.01:
            result.rejected.append({'reason':'invalid_or_ambiguous_lane_spans'})
            result.warnings.append('lane_profile_rejected'); return result
        previous_end=end
        if span['status'] in ('dedicated','segregated'): excluded.append((start,min(total,end)))
    result.applicable_m=total-sum(end-start for start,end in excluded)
    if segment.road_status=='unknown': result.warnings.append('road_status_unknown')
    reason='ambiguous_geometry' if segment.ambiguous else ('shape_unavailable' if len(segment.geometry)<2 else ('zero_length' if total<=0 else ('missing_time_baseline' if segment.moving_seconds is None or segment.moving_seconds<0 else None)))
    if reason:
        result.rejected.append({'reason':reason}); return result
    candidates=[]
    for observation in observations:
        if deadline is not None and clock()>=deadline:
            result.warnings.append('deadline_reached'); break
        if not observation.fresh(now):
            result.rejected.append({'observation_id':observation.id,'reason':'stale_observation'}); continue
        road_length=length(observation.geometry)
        if not road_length: continue
        matches=oriented_overlap(segment.geometry,observation.geometry,max_distance,max_angle,deadline,clock)
        if not matches: result.rejected.append({'observation_id':observation.id,'reason':'no_unambiguous_oriented_overlap'})
        for start,end,rs,re,error in matches:
            candidates.append((start,end,error,observation, (re-rs)/(end-start)/road_length))
    bounds=sorted({p for c in candidates for p in c[:2]} | {p for interval in excluded for p in interval})
    for start,end in zip(bounds,bounds[1:]):
        if any(start>=a-1e-6 and end<=b+1e-6 for a,b in excluded): continue
        if deadline is not None and clock()>=deadline:
            result.warnings.append('deadline_reached'); break
        choices=[c for c in candidates if c[0]<=start+1e-6 and c[1]>=end-1e-6]
        if not choices: continue
        choices.sort(key=lambda c:(c[2],-c[3].received_at,c[3].provider,c[3].id))
        chosen=choices[0]; observation=chosen[3]; size=end-start
        # Equidistant but physically distinct roads are ambiguous. Repeated
        # snapshots of the same road remain ordinary source conflicts.
        ambiguous=False
        for other in choices[1:]:
            if abs(chosen[2]-other[2])>1: continue
            center=slice_line(segment.geometry,(start+end)/2,min(end,(start+end)/2+0.01))
            if not center: continue
            point=center[0]
            def nearest(line):
                values=[]
                for a,b in zip(line,line[1:]):
                    t,error=project(point,a,b)
                    values.append((error,(a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1]))))
                return min(values)[1]
            if distance(nearest(observation.geometry),nearest(other[3].geometry))>2:
                ambiguous=True; break
        if ambiguous:
            result.rejected.append({'reason':'ambiguous_parallel_roads','start_m':start,'end_m':end}); continue
        baseline=segment.moving_seconds*size/total
        car=observation.duration_seconds*size*chosen[4]
        added=max(0,car-baseline)
        result.covered_m+=size; result.added_seconds+=added
        result.fragments.append(dict(start_m=start,end_m=end,observation_id=observation.id,provider=observation.provider,baseline_seconds=baseline,car_seconds=car,added_seconds=added,interpolated=True,age_seconds=now-observation.received_at,selection_order='geometry_error,freshness,provider,id',conflicting_observation_ids=[c[3].id for c in choices[1:] if c[3].id!=observation.id]))
    return result
