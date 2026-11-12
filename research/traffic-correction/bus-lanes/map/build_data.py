"""Offline only: reconstruct saved live-result intervals; never calls providers."""
import json,sys
from pathlib import Path
root=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(root/'backend'))
from app.traffic_graph import TrafficGraph,_PinnedSnapshot
from app.traffic_geometry import slice_line
base=Path(__file__).resolve().parent.parent
sha='3b176d72280719f5e6f5fd8532a33e4e65e475acef9ad2419b61302822209295'
graph=TrafficGraph(_PinnedSnapshot(Path('/tmp/peresadochnaya-gtfs-index')/f'v1-{sha}.sqlite3',sha))
names=[('live-search','Московский вокзал → Лафонская, автобус 74'),('live-search-pionerskaya-rodeo','Пионерская → Родео Драйв'),('live-search-moskovskaya-pulkovo','Московская → Пулково')]
result=[]
for directory,title in names:
 file=base/directory/'response-corrected-full.json'
 if not file.exists(): continue
 data=json.loads(file.read_text()); route=next((r for r in data['routes'] if any(l['mode'] in ('bus','trolleybus') for l in r['legs'])),None)
 if route is None:result.append({'directory':directory,'title':title,'no_bus_candidate':True});continue
 entry={'directory':directory,'title':title,'route_id':route['id'],'route_name':route['name'],'duration_seconds':route['duration_seconds'],'debug':route['debug'],'legs':[],'protected':[],'traffic_covered':[],'unknown':[],'protected_m':0}
 for leg in route['legs']:
  entry['legs'].append({'mode':leg['mode'],'geometry':leg['geometry']['coordinates'],'line':leg.get('line')})
  if leg['mode'] not in ('bus','trolleybus'):continue
  trip=leg['line']['trip_id'].split(':')[-1]; segments=graph.trip_segments(trip)
  for debug in (leg.get('debug') or {}).get('segments',[]):
   seq=int(debug['segment_id'].split(':')[-1]);seg=next((s for s in segments if s.from_stop_id==debug['from_stop_id'] and s.to_stop_id==debug['to_stop_id'] and s.from_sequence==seq),None)
   if seg is None: raise ValueError(('missing segment',debug['segment_id'],trip))
   protected=debug.get('lane_spans',[]);covered=debug.get('fragments',[])
   for span in protected:
    line=slice_line(seg.geometry,span['start_m'],span['end_m']);entry['protected'].append(list(line));entry['protected_m']+=span['end_m']-span['start_m']
   for span in covered:entry['traffic_covered'].append(list(slice_line(seg.geometry,span['start_m'],span['end_m'])))
   # Complement of both known traffic fragments and protected lane intervals.
   from app.traffic_geometry import length
   size=length(seg.geometry);intervals=sorted((max(0,s['start_m']),min(size,s['end_m'])) for s in protected+covered);offset=0
   for a,b in intervals:
    if a>offset:entry['unknown'].append(list(slice_line(seg.geometry,offset,a)))
    offset=max(offset,b)
   if offset<size:entry['unknown'].append(list(slice_line(seg.geometry,offset,size)))
 result.append(entry)
(base/'map'/'route-map-data.json').write_text(json.dumps(result,ensure_ascii=False,separators=(',',':'))+'\n');print([(r['title'],r.get('protected_m'),len(r.get('unknown',[]))) for r in result])
