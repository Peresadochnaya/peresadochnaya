"""Check both public entrypoints without calling paid routing providers."""

import json
import sys
import time
import urllib.error
import urllib.request


PUBLIC_URLS = (
    "https://peresadochnaya.lol",
    "https://peresadochnaya.rjnk.cc",
)
PATHS = (
    "/health/ready",
    "/",
    "/api/v1/data/status",
    "/api/v1/raw_feed",
    "/api/v1/vehicles?dummy=false&bbox=29,59,31,61&limit=1",
)


def validate(path, body, content_type):
    if path == "/":
        if "text/html" not in content_type or b'<div id="root"' not in body:
            raise ValueError("expected the application frontend")
    elif path == "/api/v1/raw_feed":
        if not body or "application/octet-stream" not in content_type:
            raise ValueError("expected a nonempty binary GTFS-RT feed")
    else:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        if path == "/health/ready" and data.get("status") != "ready":
            raise ValueError("backend is not ready")


def check(url, path, *, attempts=7, delay=5, timeout=15):
    """Retry rollout/transient failures, but fail CI when an endpoint stays broken."""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url + path, timeout=timeout) as response:
                validate(path, response.read(), response.headers.get("Content-Type", ""))
            print(f"OK {url}{path}", flush=True)
            return
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if attempt + 1 == attempts:
                raise RuntimeError(f"{url}{path}: {exc}") from exc
            print(f"Retry {attempt + 1}/{attempts}: {url}{path}: {exc}", flush=True)
            time.sleep(delay)


def main(urls=None):
    # Keep both production domains even when API_BASE_URL selects another URL.
    urls = dict.fromkeys(url.rstrip("/") for url in [*(urls or []), *PUBLIC_URLS])
    failed = False
    for url in urls:
        try:
            for path in PATHS:
                check(url, path)
        except RuntimeError as exc:
            print(f"FAIL {exc}", file=sys.stderr, flush=True)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
