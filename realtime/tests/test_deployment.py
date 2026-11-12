from pathlib import Path
import json
import yaml

ROOT=Path(__file__).parents[2]


def test_separate_image_single_producer_persistent_feed_and_ingress():
    objects=list(yaml.safe_load_all((ROOT/'deploy/k8s/realtime.yaml').read_text()))
    deployment=objects[0];spec=deployment['spec'];pod=spec['template']['spec']
    assert spec['replicas']==1 and spec['strategy']['type']=='Recreate'
    assert 'peresadochnaya:realtime-' in pod['containers'][0]['image']
    assert any(v.get('persistentVolumeClaim',{}).get('claimName')=='realtime-feed' for v in pod['volumes'])
    ingress=yaml.safe_load((ROOT/'deploy/k8s/ingress.yaml').read_text())
    raw=next(p for p in ingress['spec']['rules'][0]['http']['paths'] if p['path']=='/api/v1/raw_feed')
    assert raw['pathType']=='Exact' and raw['backend']['service']['name']=='realtime'
    updater=json.loads((ROOT/'otp/config/router-config.json').read_text())['updaters'][0]
    assert updater=={'type':'stop-time-updater','feedId':'2','url':'http://realtime:8001/api/v1/raw_feed','frequency':'10s'}
    workflow=yaml.safe_load((ROOT/'.github/workflows/backend.yml').read_text())
    assert any(step.get('with',{}).get('file')=='realtime/Dockerfile' for step in workflow['jobs']['image']['steps'])
