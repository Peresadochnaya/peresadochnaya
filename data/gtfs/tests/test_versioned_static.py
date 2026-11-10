import json
import shutil
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from data.gtfs import versioned_static as store
from data.gtfs.tests.test_refresh_static import make_feed


class Response:
    def __init__(self, content=b"", code=200, headers=None):
        self.content, self.status_code, self.headers = content, code, headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def iter_content(self, chunk_size): yield self.content


class VersionedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"
        self.feed = Path(self.temp.name) / "feed.zip"
        make_feed(self.feed, 2)

    def test_publish_invalid_calendar_is_saved_but_unusable(self):
        self.root.mkdir()
        result = store.publish(self.root, self.feed, "20261110", "now")
        self.assertFalse(result["usable"])
        version = self.root / "versions" / result["sha256"]
        self.assertTrue((version / "feed.zip").is_file())
        self.assertTrue((version / "tables/stops.csv").is_file())
        self.assertEqual(store.read_json(self.root / "current.json")["sha256"], result["sha256"])
        with patch.object(store, "prepare_csv", side_effect=AssertionError("must not extract")):
            self.assertEqual(store.publish(self.root, self.feed, "20261111", "later")["status"], "unchanged")

    def test_conditional_request_and_failed_download_preserve_current(self):
        calls = []
        responses = iter([Response(self.feed.read_bytes(), headers={"ETag": "v1"}), Response(code=304)])
        def get(*args, **kwargs):
            calls.append(kwargs)
            return next(responses)
        first = store.refresh(self.root, service_date="20261110", get=get)
        second = store.refresh(self.root, service_date="20261111", get=get)
        self.assertEqual(calls[1]["headers"], {"If-None-Match": "v1"})
        self.assertEqual(second["service_date"], "20261111")
        pointer = store.read_json(self.root / "current.json")
        with self.assertRaises(ValueError):
            store.refresh(self.root, get=lambda *a, **k: Response(b"bad zip"))
        self.assertEqual(store.read_json(self.root / "current.json"), pointer)
        status = store.read_json(self.root / "refresh-status.json")
        self.assertEqual(status["status"], "error")
        self.assertEqual(status["last_success_at"], second["last_success_at"])

    def test_successive_refreshes_keep_only_latest_snapshot(self):
        self.root.mkdir()
        versions = []
        for count in range(1, 5):
            make_feed(self.feed, count)
            versions.append(store.refresh(self.root, service_date="20261110", get=lambda *a, **k: Response(self.feed.read_bytes()))["sha256"])
            self.assertEqual([p.name for p in (self.root / "versions").iterdir()], [versions[-1]])

    def test_lock_prevents_overlap(self):
        with store.store_lock(self.root):
            with self.assertRaises(RuntimeError):
                with store.store_lock(self.root):
                    self.fail("second lock acquired")

    def test_unchanged_feed_recalculates_calendar_after_midnight(self):
        with zipfile.ZipFile(self.feed) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files["calendar.txt"] = files["calendar.txt"].replace(b"20261110,20191230", b"20261110,20261110")
        with zipfile.ZipFile(self.feed, "w") as archive:
            for name, content in files.items(): archive.writestr(name, content)
        first = store.refresh(self.root, service_date="20261110",
                              get=lambda *a, **k: Response(self.feed.read_bytes()))
        self.assertTrue(first["usable"])
        before = datetime.fromisoformat("2026-11-10T23:59:59+03:00")
        after = datetime.fromisoformat("2026-11-11T00:00:01+03:00")
        with patch.object(store, "datetime") as clock:
            clock.now.side_effect = [before, after]
            second = store.refresh(self.root, get=lambda *a, **k: Response(code=304))
        self.assertEqual(second["status"], "unchanged")
        self.assertFalse(second["usable"])
        self.assertEqual(second["service_date"], "20261111")
        self.assertTrue(store.read_json(self.root / "versions" / first["sha256"] / "manifest.json")["usable"])

    def test_failed_refresh_retains_only_latest_snapshot(self):
        self.root.mkdir()
        for count in range(1, 4):
            make_feed(self.feed, count)
            store.refresh(self.root, service_date="20261110", get=lambda *a, **k: Response(self.feed.read_bytes()))
        latest = store.read_json(self.root / "current.json")
        with self.assertRaises(ValueError): store.refresh(self.root, get=lambda *a, **k: Response(b"broken"))
        self.assertEqual(store.read_json(self.root / "current.json"), latest)
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()], [latest["sha256"]])


if __name__ == "__main__": unittest.main()
