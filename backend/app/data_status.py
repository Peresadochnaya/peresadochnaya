import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .schemas import DataStatus


def read_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return value


def load_graph(root: Path, result: DataStatus) -> None:
    """Read the self-contained graph metadata published by the OTP builder."""
    graph = read_object(root / "otp" / "current.json")
    result.graph_check = read_object(root / "otp" / "build-status.json")
    if not graph:
        return
    version = graph.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("Invalid graph version")
    source = graph.get("gtfs_source_sha256")
    if source is not None and (not isinstance(source, str)
                               or not re.fullmatch(r"[0-9a-f]{64}", source)):
        raise ValueError("Invalid graph GTFS version")
    for key in ("service_start", "service_end", "activated_at"):
        value = graph.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Invalid graph field: {key}")
    result.graph_version = version
    result.graph = graph


def load_status(root: Path) -> DataStatus:
    """Read self-contained, atomically published current metadata.

    No second read through a version path: the updater may delete the old
    payload immediately after publishing a new current.json.
    """
    result = DataStatus(state="missing")
    try:
        result.last_check = read_object(root / "gtfs" / "refresh-status.json")
        current = read_object(root / "gtfs" / "current.json")
        load_graph(root, result)
        if not current:
            result.warnings.append("GTFS has not been downloaded")
            return result
        sha = current.get("sha256", "")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("Invalid GTFS version pointer")
        result.gtfs_version = sha
        # Keep the public manifest field, sourced from the same atomic document
        # as gtfs_version instead of a separately deletable directory.
        result.manifest = current
        active = result.manifest.get("active_service_ids")
        service_date = result.manifest.get("service_date")
        if active is not None and (type(active) is not int or active < 0):
            raise ValueError("Invalid active_service_ids")
        if service_date is not None and (not isinstance(service_date, str) or not re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", service_date)):
            raise ValueError("Invalid service_date")
        result.state = "ready"
        if result.manifest.get("active_service_ids") == 0:
            result.warnings.append("GTFS has no active services on its validation date")
        if result.last_check and result.last_check.get("status") == "error":
            result.warnings.append("Latest GTFS refresh failed; previous version retained")
        if result.graph:
            source = result.graph.get("gtfs_source_sha256")
            if source and source != sha:
                result.warnings.append("Graph was built from a different GTFS version")
            if not result.graph.get("activated_at"):
                result.warnings.append("Graph has not been confirmed as loaded by the planner")
            service_end = result.graph.get("service_end")
            if isinstance(service_end, str) and len(service_end) >= 10:
                today = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")
                if service_end[:10] < today:
                    result.warnings.append("Graph service period has ended")
        if result.graph_check and result.graph_check.get("status") == "error":
            result.warnings.append("Latest graph build failed; previous version retained")
        if result.warnings:
            result.state = "degraded"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        result.state = "degraded"
        result.graph = None
        result.graph_version = None
        result.warnings.append("Data status is unavailable or invalid")
    return result
