"""Fast offline builder checks; no PBF scan or external calls."""
import json
from pathlib import Path

import pytest

from app.traffic_lane_builder import geometry_fingerprint, match_spans, publish_profile

LINE = ((30., 60.), (30.01, 60.))


def lane(identifier, latitude=60.):
    return {'osm_way_id': identifier, 'osm_version': 3,
            'date': '2025-04-14T12:00:00Z', 'direction': 1,
            'source': f'https://www.openstreetmap.org/way/{identifier}/history',
            'geometry': ((30., latitude), (30.01, latitude))}


def test_equal_parallel_carriageways_cannot_establish_lane_identity():
    assert match_spans(LINE, [lane(1, 59.99995), lane(2, 60.00005)]) == []


def test_input_permutation_cannot_change_unambiguous_spans():
    nearest, farther = lane(1), lane(2, 60.0001)
    assert match_spans(LINE, [nearest, farther]) == match_spans(LINE, [farther, nearest])
    assert match_spans(tuple(reversed(LINE)), [nearest]) == []


def test_publication_is_deterministic_and_complete(tmp_path):
    path=tmp_path/'road-status.json'
    data={'version':'test','edges':[{'spans':[]}],'feed_sha256':{'gtfs':'a'*64}}
    publish_profile(path,data); first=path.read_bytes()
    publish_profile(path,data)
    assert path.read_bytes()==first
    assert json.loads(first)==data
    assert not path.with_suffix('.json.tmp').exists()


def test_failed_replace_preserves_published_profile(tmp_path,monkeypatch):
    path=tmp_path/'road-status.json'; path.write_text('previous complete profile')
    def fail_replace(self,target): raise OSError('simulated publication interruption')
    monkeypatch.setattr(Path,'replace',fail_replace)
    with pytest.raises(OSError): publish_profile(path,{'version':'replacement'})
    assert path.read_text()=='previous complete profile'
    assert not path.with_suffix('.json.tmp').exists()


def test_real_profile_has_exact_unique_variants_and_pinned_feed():
    path=Path(__file__).parents[1]/'app/fixtures/traffic/road-status.json'
    profile=json.loads(path.read_text())
    assert profile['feed_sha256']['gtfs']=='3b176d72280719f5e6f5fd8532a33e4e65e475acef9ad2419b61302822209295'
    assert len(profile['edges'])>600
    count=0
    for row in profile['edges']:
        variants=row['variants']; hashes=[v['geometry_sha256'] for v in variants]
        assert len(hashes)==len(set(hashes))
        assert row['geometry_sha256'] in hashes
        for variant in variants:
            assert variant['source'] and variant['date'] and variant['spans']
            assert len(variant['geometry_sha256'])==64
            assert all(s['end_m']>s['start_m']>=0 for s in variant['spans'])
        count+=len(variants)
    assert count==profile['summary']['geometry_variants']
    assert profile['source_metadata']['snapshot_at'] is None
    assert 'last edit' in profile['source_metadata']['way_date_semantics']
    assert geometry_fingerprint(LINE)!=geometry_fingerprint(tuple(reversed(LINE)))


def test_official_supplement_keeps_source_and_directed_extent(tmp_path):
    from app.traffic_lane_builder import load_supplemental
    props=lane(101)
    geometry=props.pop('geometry')
    props.update(source_kind='official_inventory',official_corridor_id='085',
                 source='https://gov.spb.ru/example',boundary_provenance={'from':'boundary A','to':'boundary B'})
    path=tmp_path/'official.geojson'
    path.write_text(json.dumps({'type':'FeatureCollection','features':[{
        'type':'Feature','properties':props,'geometry':{'type':'LineString','coordinates':geometry}}]}))
    lanes=load_supplemental(path)
    spans=match_spans(LINE,lanes)
    assert spans and spans[0]['source']=='https://gov.spb.ru/example'
    assert spans[0]['official_corridor_id']=='085'
    assert spans[0]['source_kind']=='official_inventory'
    assert match_spans(tuple(reversed(LINE)),lanes)==[]
    props.pop('boundary_provenance')
    path.write_text(json.dumps({'type':'FeatureCollection','features':[{
        'type':'Feature','properties':props,'geometry':{'type':'LineString','coordinates':geometry}}]}))
    with pytest.raises(ValueError,match='provenance'): load_supplemental(path)


def test_reviewed_malaya_morskaya_has_exact_boundaries_and_counterflow():
    from app.traffic_lane_builder import load_supplemental
    from app.traffic_geometry import length
    path=Path(__file__).parents[1]/'app/fixtures/traffic/official-resolved-lanes.geojson'
    lanes=load_supplemental(path)
    selected=[lane for lane in lanes if lane['official_corridor_id']=='spb-official-045']
    assert selected
    assert sum(length(lane['geometry']) for lane in selected)==pytest.approx(500,abs=2)
    endpoints=[]
    for item in selected:
        assert item['direction']==-1 and item['tags']['oneway']=='yes'
        assert item['source'].startswith('https://www.gov.spb.ru/')
        assert item['boundary_provenance']['path_count']==1
        endpoints.extend((tuple(item['geometry'][0]),tuple(item['geometry'][-1])))
    from collections import Counter
    termini={point for point,count in Counter(endpoints).items() if count==1}
    assert termini=={(30.308106,59.9339456),(30.3151542,59.9366657)}


def test_adjacent_official_pieces_keep_individual_osm_identity():
    a=lane(1); b=lane(2)
    a['geometry']=((30.,60.),(30.005,60.))
    b['geometry']=((30.005,60.),(30.01,60.))
    for item in (a,b):
        item.update(source='https://www.gov.spb.ru/inventory',source_kind='official_inventory',
                    official_corridor_id='test',boundary_provenance={'reviewed':True})
    spans=match_spans(LINE,[a,b])
    assert len(spans)==2
    assert [s['osm_way_id'] for s in spans]==[1,2]
    assert spans[0]['end_m']==pytest.approx(spans[1]['start_m'])
