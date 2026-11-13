"""Offline, reproducible explicit OSM bus-lane to directed GTFS shape mapping.

Run from backend: python -m app.traffic_lane_builder --osm ../spb-lo.osm.pbf
--index /data/gtfs-index/v1-SHA.sqlite3 --output app/fixtures/traffic/road-status.json
Requires optional build dependency osmium (runtime has no OSM dependency).
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
from .traffic_geometry import oriented_overlap, length


def geometry_fingerprint(line):
    return hashlib.sha256(json.dumps([[round(x,7),round(y,7)] for x,y in line],separators=(',',':')).encode()).hexdigest()


def dedicated_directions(tags):
    """Only explicit exclusive access; bus=yes alone is not a dedicated lane."""
    if any('conditional' in key or key=='opening_hours' for key in tags): return ()
    oneway=tags.get('oneway') in ('yes','1','true')
    reverse=tags.get('oneway')=='-1'
    allowed=(-1,) if reverse else ((1,) if oneway else (1,-1))
    result=set()
    for direction,suffix in ((1,'forward'),(-1,'backward')):
        if direction not in allowed and tags.get('oneway:bus')!='no' and tags.get('oneway:psv')!='no': continue
        for vehicle in ('bus','psv'):
            value=tags.get(f'{vehicle}:lanes:{suffix}')
            if value and any(v in ('designated','exclusive') for v in value.split('|')): result.add(direction)
            # Generic lanes on a two-way road have no reliable orientation.
            if len(allowed)==1 and any(v in ('designated','exclusive') for v in tags.get(f'{vehicle}:lanes','').split('|')): result.add(direction)
        if len(allowed)==1 and tags.get('highway') in ('busway',): result.add(direction)
        if len(allowed)==1 and tags.get('access') in ('no','private') and tags.get('bus') in ('yes','designated') and tags.get('highway'): result.add(direction)
    # Legacy busway=lane is exclusive by definition, direction explicit on two-way.
    if tags.get('busway')=='lane' and len(allowed)==1: result.update(allowed)
    for side,direction in (('right',1),('left',-1)):
        value=tags.get('busway:'+side)
        if value=='lane': result.update(allowed if len(allowed)==1 else (direction,))
        if value=='opposite_lane' and len(allowed)==1: result.add(-allowed[0])
    if tags.get('busway')=='opposite_lane' and len(allowed)==1: result.add(-allowed[0])
    return tuple(sorted(result))


def source_metadata(path):
    """Read authoritative PBF header only; filesystem times are not observations."""
    import osmium
    reader=osmium.io.Reader(str(path),osmium.osm.osm_entity_bits.NOTHING)
    try:
        header=reader.header()
        return {'format':'OpenStreetMap PBF','generator':header.get('generator') or None,
                'snapshot_at':header.get('osmosis_replication_timestamp') or None,
                'replication_sequence':header.get('osmosis_replication_sequence_number') or None,
                'replication_base_url':header.get('osmosis_replication_base_url') or None,
                'way_date_semantics':'OSM element last edit timestamp; not field observation or snapshot date'}
    finally: reader.close()


def publish_profile(output,data):
    output=Path(output); output.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_suffix('.json.tmp')
    try:
        temporary.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':'))+'\n')
        temporary.replace(output)
    finally: temporary.unlink(missing_ok=True)


def extract_lanes(path):
    import osmium
    lanes=[]
    class Handler(osmium.SimpleHandler):
        def way(self,way):
            tags=dict(way.tags); directions=dedicated_directions(tags)
            if not directions: return
            try: line=[(n.lon,n.lat) for n in way.nodes]
            except osmium.InvalidLocationError: return
            if len(line)<2: return
            for direction in directions:
                lanes.append({'osm_way_id':way.id,'osm_version':way.version,'date':str(way.timestamp),'direction':direction,'tags':tags,'geometry':line if direction==1 else list(reversed(line)),'source':f'https://www.openstreetmap.org/way/{way.id}/history'})
    handler=Handler(); handler.apply_file(str(path),locations=True,idx='flex_mem')
    return lanes


def match_spans(line,lanes):
    candidates=[]
    for lane in lanes:
        for start,end,_,_,error in oriented_overlap(line,lane['geometry'],20,30):
            if end-start>=3: candidates.append((start,end,error,lane))
    bounds=sorted({x for c in candidates for x in c[:2]}); result=[]
    for start,end in zip(bounds,bounds[1:]):
        choices=sorted((c for c in candidates if c[0]<=start+1e-6 and c[1]>=end-1e-6),key=lambda c:(c[2],c[3]['osm_way_id']))
        if not choices: continue
        # Equally plausible parallel ways cannot establish carriageway identity.
        if any(abs(c[2]-choices[0][2])<1 and c[3]['osm_way_id']!=choices[0][3]['osm_way_id'] for c in choices[1:]): continue
        lane=choices[0][3]
        span={'start_m':start,'end_m':end,'status':'dedicated','source':lane['source'],'date':lane['date'],'osm_way_id':lane['osm_way_id'],'osm_version':lane['osm_version'],'direction':lane['direction']}
        for field in ('source_kind','official_corridor_id','boundary_provenance'):
            if field in lane: span[field]=lane[field]
        if result and result[-1]['source']==span['source'] and result[-1]['osm_way_id']==span['osm_way_id'] and abs(result[-1]['end_m']-start)<.001: result[-1]['end_m']=end
        else: result.append(span)
    return result


def load_supplemental(path):
    """Load separately reviewed official corridor geometry, without inventing OSM tags."""
    data=json.loads(Path(path).read_text()); lanes=[]
    if data.get('type')!='FeatureCollection': raise ValueError('Invalid supplemental layer')
    for feature in data['features']:
        props=dict(feature['properties']); geometry=feature['geometry']
        if geometry.get('type')!='LineString' or len(geometry['coordinates'])<2: raise ValueError('Invalid supplemental geometry')
        if props.get('source_kind')!='official_inventory' or not all(props.get(k) for k in ('source','date','official_corridor_id','boundary_provenance')):
            raise ValueError('Supplemental official provenance missing')
        if props.get('direction') not in (-1,1): raise ValueError('Supplemental travel direction missing')
        if not isinstance(props.get('osm_way_id'),int) or not isinstance(props.get('osm_version'),int): raise ValueError('Supplemental underlying OSM identity missing')
        for point in geometry['coordinates']:
            if len(point)!=2 or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in point): raise ValueError('Invalid supplemental coordinate')
        lanes.append(dict(props,geometry=geometry['coordinates']))
    return lanes


def build_profile(osm_path,index_path,output,supplemental=None):
    from .traffic_graph import TrafficGraph,_PinnedSnapshot
    osm_digest=hashlib.file_digest(open(osm_path,'rb'),'sha256').hexdigest()
    if supplemental:
        pin=json.loads(Path(supplemental).read_text()).get('osm_sha256')
        if pin!=osm_digest: raise ValueError('Official geometry underlying OSM extract mismatch')
    osm_lanes=extract_lanes(osm_path)
    official_lanes=load_supplemental(supplemental) if supplemental else []
    lanes=osm_lanes+official_lanes
    grid={}
    def cells(line):
        # 0.001 degree grid; conservatively expanded by more than 20m in SPb.
        x=[p[0] for p in line]; y=[p[1] for p in line]
        for i in range(math.floor((min(x)-.0005)*1000),math.floor((max(x)+.0005)*1000)+1):
            for j in range(math.floor((min(y)-.0003)*1000),math.floor((max(y)+.0003)*1000)+1): yield i,j
    for i,lane in enumerate(lanes):
        for cell in cells(lane['geometry']): grid.setdefault(cell,set()).add(i)
    import sqlite3
    db=sqlite3.connect(index_path)
    digest=db.execute("SELECT value FROM metadata WHERE key='source'").fetchone()[0]
    graph=TrafficGraph(_PinnedSnapshot(Path(index_path),digest))
    # Same shape and stop pattern is equivalent across departure times.
    trips=db.execute("SELECT MIN(trip_id) FROM gtfs_trips GROUP BY shape_id,route_id,direction_id ORDER BY shape_id,route_id,direction_id").fetchall()
    edges={}; conflicts=set()
    for number,(trip,) in enumerate(trips):
        if number%100==0: print(f'{number}/{len(trips)} patterns, {len(edges)} lane edges',flush=True)
        for segment in graph.trip_segments(trip):
            if segment.mode not in ('bus','trolleybus') or len(segment.geometry)<2: continue
            key=segment.edge_key; fingerprint=geometry_fingerprint(segment.geometry)
            if key in edges and fingerprint in edges[key]: continue
            selected=set()
            for cell in cells(segment.geometry): selected.update(grid.get(cell,()))
            spans=match_spans(segment.geometry,[lanes[i] for i in selected])
            if spans:
                edges.setdefault(key,{})[fingerprint]={'feed_id':'gtfs','from_stop_id':key[1],'to_stop_id':key[2],'status':'unknown','source':spans[0]['source'],'date':spans[0]['date'],'geometry_sha256':fingerprint,'spans':spans}
    rows=[]
    for key,variants in sorted(edges.items()):
        row=dict(next(iter(variants.values())))
        row['variants']=[{'geometry_sha256':v['geometry_sha256'],'spans':v['spans'],'source':v['source'],'date':v['date']} for _,v in sorted(variants.items())]
        rows.append(row)
    data={'version':'osm-and-official-bus-lanes-v2','feed_sha256':{'gtfs':digest},'source_metadata':source_metadata(osm_path),'osm_sha256':osm_digest,'matching':{'distance_m':20,'angle_degrees':30,'minimum_overlap_m':3},'edges':rows,'summary':{'osm_directed_ways':len(osm_lanes),'official_directed_fragments':len(official_lanes),'gtfs_patterns':len(trips),'directed_edges':len(rows),'dedicated_m':sum(s['end_m']-s['start_m'] for r in rows for v in r['variants'] for s in v['spans']),'geometry_variants':sum(len(r['variants']) for r in rows),'coverage_unit':'sum of directed GTFS geometry variant metres, not physical city lane length'}}
    if supplemental:
        data['supplemental_sha256']=hashlib.file_digest(open(supplemental,'rb'),'sha256').hexdigest()
        data['source_metadata']['official_date_semantics']='Official inventory publication date; geometry extent separately resolved against pinned OSM extract'
    output=Path(output); publish_profile(output,data)
    output.with_name('osm-bus-lanes.geojson').write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','geometry':{'type':'LineString','coordinates':l.pop('geometry')},'properties':l} for l in osm_lanes]},ensure_ascii=False,separators=(',',':'))+'\n')
    print(data['summary'],flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--osm',required=True,type=Path); parser.add_argument('--index',required=True,type=Path); parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--supplemental',type=Path,default=None)
    args=parser.parse_args(); build_profile(args.osm,args.index,args.output,args.supplemental)
