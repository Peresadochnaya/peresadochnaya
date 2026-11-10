"""Atomic, versioned GTFS storage shared by the updater and read-only API."""

import fcntl
import argparse
import sys
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .refresh_static import CA_BUNDLE, URL, inspect_archive, prepare_csv, sha256


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            json.dump(value, target, ensure_ascii=False)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def store_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("GTFS refresh already running") from exc
        yield


def publish(root, incoming, service_date, checked_at):
    """Publish a whole version before replacing the reader's pointer."""
    root = Path(root)
    digest = sha256(incoming)
    details = inspect_archive(incoming, service_date)
    details["usable"] = details["active_service_ids"] > 0
    destination = root / "versions" / digest
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        staging = Path(tempfile.mkdtemp(prefix=".version-", dir=destination.parent))
        try:
            shutil.copyfile(incoming, staging / "feed.zip")
            prepare_csv(staging / "feed.zip", staging / "tables", digest)
            tables = read_json(staging / "tables" / "manifest.json")
            atomic_json(staging / "manifest.json", {
                "sha256": digest, "source_sha256": digest, "created_at": checked_at,
                **details, "files": tables["files"],
            })
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    previous = read_json(root / "current.json")
    pointer = {"sha256": digest, "checked_at": checked_at, **details}
    atomic_json(root / "current.json", pointer)
    return {"status": "unchanged" if previous.get("sha256") == digest else "updated", **pointer}


def prune(root):
    """Keep only the successfully published current snapshot."""
    root = Path(root)
    protected = {read_json(root / "current.json").get("sha256")}
    versions = sorted(
        (p for p in (root / "versions").iterdir() if p.is_dir() and re.fullmatch(r"[a-f0-9]{64}", p.name)),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for path in versions:
        if path.name not in protected:
            shutil.rmtree(path)


def refresh(root, *, service_date=None, ca_bundle=CA_BUNDLE, get=requests.get):
    root = Path(root)
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    checked_at = now.isoformat()
    with store_lock(root):
        previous = read_json(root / "refresh-status.json")
        status = {**previous, "attempted_at": checked_at}
        atomic_json(root / "refresh-status.json", {**status, "status": "running"})
        incoming = None
        try:
            current = read_json(root / "current.json")
            headers = {}
            archive = root / "versions" / str(current.get("sha256", "")) / "feed.zip"
            if archive.is_file():
                if previous.get("etag"):
                    headers["If-None-Match"] = previous["etag"]
                if previous.get("last_modified"):
                    headers["If-Modified-Since"] = previous["last_modified"]
            with get(URL, headers=headers, stream=True, timeout=(10, 120), verify=str(ca_bundle)) as response:
                response.raise_for_status()
                if response.status_code == 304:
                    if not archive.is_file():
                        raise ValueError("Server returned 304 without a local feed")
                    source = archive
                else:
                    with tempfile.NamedTemporaryFile(dir=root, suffix=".zip", delete=False) as target:
                        incoming = Path(target.name)
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                target.write(chunk)
                    source = incoming
                # A slow download can cross midnight. Evaluate the calendar for
                # the date on which we publish, rather than yesterday's attempt.
                completed_at = datetime.now(ZoneInfo("Europe/Moscow"))
                checked_at = completed_at.isoformat()
                result = publish(root, source,
                                 service_date or completed_at.strftime("%Y%m%d"), checked_at)
                status.update(result)
                status.update(etag=response.headers.get("ETag", previous.get("etag") if response.status_code == 304 else None),
                              last_modified=response.headers.get("Last-Modified", previous.get("last_modified") if response.status_code == 304 else None))
            status.update(last_success_at=checked_at, error=None)
            atomic_json(root / "refresh-status.json", status)
            prune(root)
            return status
        except Exception as exc:
            atomic_json(root / "refresh-status.json", {**status, "status": "error", "error": str(exc)})
            raise
        finally:
            if incoming is not None:
                incoming.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Обновить серверный снимок GTFS")
    parser.add_argument("--store-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(refresh(args.store_dir), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        print(f"Ошибка обновления GTFS: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
