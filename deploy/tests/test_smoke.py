import importlib.util
import io
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import pytest
import yaml


DEPLOY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_smoke", DEPLOY / "smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_both_hosts_route_api_and_raw_feed():
    ingress = yaml.safe_load((DEPLOY / "k8s/ingress.yaml").read_text())
    rules = {r["host"]: r["http"]["paths"] for r in ingress["spec"]["rules"]}
    assert set(rules) == {"peresadochnaya.lol", "peresadochnaya.rjnk.cc"}
    for paths in rules.values():
        routes = {p["path"]: p for p in paths}
        assert routes["/api/v1/raw_feed"]["pathType"] == "Exact"
        assert routes["/api/v1/raw_feed"]["backend"]["service"]["name"] == "realtime"
        assert routes["/"]["pathType"] == "Prefix"
        assert routes["/"]["backend"]["service"]["name"] == "api"
    # NPM terminates .lol TLS; only the origin can renew certificates in k3s.
    assert ingress["spec"]["tls"] == [{"hosts": ["peresadochnaya.rjnk.cc"]}]


@pytest.mark.parametrize(
    "path,body,content_type",
    [
        ("/", b'<html><div id="root"></div></html>', "text/html; charset=utf-8"),
        ("/health/ready", b'{"status":"ready"}', "application/json"),
        ("/api/v1/raw_feed", b"\x0a\x02\x10\x02", "application/octet-stream"),
        ("/api/v1/data/status", b'{}', "application/json"),
    ],
)
def test_valid_public_responses(path, body, content_type):
    smoke.validate(path, body, content_type)


@pytest.mark.parametrize(
    "path,body,content_type",
    [
        ("/", b'<html>Nginx default page</html>', "text/html"),
        ("/health/ready", b'{"status":"starting"}', "application/json"),
        ("/api/v1/raw_feed", b"", "application/octet-stream"),
        ("/api/v1/raw_feed", b"<html>Error</html>", "text/html"),
        ("/api/v1/data/status", b"404 page not found", "text/plain"),
        ("/api/v1/data/status", b"[]", "application/json"),
    ],
)
def test_rejects_broken_or_misrouted_responses(path, body, content_type):
    with pytest.raises(ValueError):
        smoke.validate(path, body, content_type)


def response():
    r = Mock()
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    r.read.return_value = b'{"status":"ready"}'
    r.headers = {"Content-Type": "application/json"}
    return r


def test_retries_transient_http_failure():
    error = HTTPError("https://test/health/ready", 503, "Starting", {}, io.BytesIO())
    with patch.object(smoke.urllib.request, "urlopen", side_effect=[error, response()]) as get:
        smoke.check("https://test", "/health/ready", attempts=2, delay=0, timeout=3)
    assert get.call_count == 2
    assert get.call_args.kwargs["timeout"] == 3


@pytest.mark.parametrize("error", [URLError("DNS failure"), TimeoutError("timed out")])
def test_fails_after_bounded_retries(error):
    with patch.object(smoke.urllib.request, "urlopen", side_effect=error) as get:
        with pytest.raises(RuntimeError, match="https://test/health/ready"):
            smoke.check("https://test", "/health/ready", attempts=2, delay=0)
    assert get.call_count == 2


def test_checks_both_domains_and_deduplicates_base_url():
    with patch.object(smoke, "check") as check:
        assert smoke.main([smoke.PUBLIC_URLS[0] + "/"]) == 0
    assert check.call_count == len(smoke.PATHS) * 2
    assert {c.args[0] for c in check.call_args_list} == set(smoke.PUBLIC_URLS)


def test_custom_base_url_does_not_remove_production_domains():
    with patch.object(smoke, "check") as check:
        assert smoke.main(["https://extra.example"]) == 0
    assert {c.args[0] for c in check.call_args_list} == {*smoke.PUBLIC_URLS, "https://extra.example"}


def test_failure_on_old_domain_fails_smoke():
    def check(url, path):
        if url == smoke.PUBLIC_URLS[1]:
            raise RuntimeError("404 page not found")

    with patch.object(smoke, "check", side_effect=check):
        assert smoke.main() == 1


def test_failure_on_new_domain_still_checks_old_domain():
    def check(url, path):
        if url == smoke.PUBLIC_URLS[0]:
            raise RuntimeError("502 Bad Gateway")

    with patch.object(smoke, "check", side_effect=check) as get:
        assert smoke.main() == 1
    assert any(c.args[0] == smoke.PUBLIC_URLS[1] for c in get.call_args_list)
