"""Static hosting must preserve API routes and use only the public map key."""
from fastapi.testclient import TestClient

from app.main import create_app


def test_frontend_and_api_share_origin_without_changing_contract(tmp_path, monkeypatch):
    (tmp_path / 'index.html').write_text('<html>Пересадочная</html>')
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets/app.js').write_text('console.log("frontend")')
    monkeypatch.setenv('FRONTEND_DIST', str(tmp_path))
    monkeypatch.setenv('YANDEX_JS_API_KEY', 'public-map-test-key')
    monkeypatch.setenv('YANDEX_ROUTER_API_KEY', 'private-router-test-key')
    with TestClient(create_app(tmp_path, 'dummy')) as client:
        assert 'Пересадочная' in client.get('/').text
        assert client.get('/assets/app.js').status_code == 200
        assert client.get('/health/live').status_code == 200
        assert client.get('/docs').status_code == 200
        assert client.get('/api/v1/ticket-profiles').status_code == 200
        assert client.get('/api/v1/unknown').status_code == 404
        response = client.get('/map-api.js', follow_redirects=False)
        assert response.status_code == 307
        assert 'public-map-test-key' in response.headers['location']
        assert 'private-router-test-key' not in response.headers['location']
        assert response.headers['cache-control'] == 'no-store'
        assert '/map-api.js' not in client.get('/openapi.json').json()['paths']


def test_missing_map_key_is_explicit_without_external_call(tmp_path, monkeypatch):
    monkeypatch.delenv('YANDEX_JS_API_KEY', raising=False)
    with TestClient(create_app(tmp_path, 'dummy')) as client:
        response = client.get('/map-api.js', follow_redirects=False)
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'map_unavailable'
