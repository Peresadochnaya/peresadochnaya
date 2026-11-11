import copy
import csv
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from data.gtfs.refresh_static import inspect_archive
from data.gtfs.versioned_static import read_json, store_lock
from data.metro.feed import build_tables, write_feed
from data.metro.refresh import fetch_sources, local_sources, refresh
from data.metro.timing import attach_timings, parse_pmetro, seconds

FIXTURE = Path(__file__).parent / "fixtures" / "Metro.trp"
REFERENCE = Path(__file__).resolve().parents[2] / "processed" / "metro"
NOW = datetime(2026, 11, 11, 12, tzinfo=ZoneInfo("Europe/Moscow"))


@pytest.fixture
def reference():
    return local_sources(REFERENCE, FIXTURE)


@pytest.mark.parametrize("value,expected", [("3.10", 190), ("2.05", 125), ("2", 120), ("0", 0), ("1.5", 65)])
def test_minutes_seconds(value, expected):
    assert seconds(value) == expected


@pytest.mark.parametrize("value", ["1.60", "-2", "1.000", "abc", ""])
def test_invalid_duration(value):
    with pytest.raises(ValueError):
        seconds(value)


def test_all_operating_connections_have_positive_times(reference):
    tables, _ = reference
    assert len(tables["stations.csv"]) == 75
    assert len(tables["segments.csv"]) == 69
    assert len(tables["transfers.csv"]) == 10
    assert all(r["duration_seconds"] > 0 for name in ("segments.csv", "transfers.csv") for r in tables[name])
    edge = next(r for r in tables["segments.csv"] if r["from_station_id"] == "metro:prb-gor")
    assert edge["to_station_id"] == "metro:prb-sps"
    assert edge["duration_seconds"] == 360  # Direct branch skips unopened Teatralnaya.


def test_zero_future_edges_and_future_stations_do_not_leak(reference):
    edges, transfers = parse_pmetro(FIXTURE.read_text())
    assert (6, "броневая", "путиловская") not in edges
    assert all(value > 0 for value in transfers.values())
    tables = build_tables(reference[0], NOW.date())
    assert not any(r["stop_name"] in ("Театральная", "Броневая", "Каретная") for r in tables["stops.txt"])


def test_missing_timing_fails_instead_of_guessing(reference):
    tables, _ = reference
    broken = FIXTURE.read_text().replace("3.10,3.40", "0,3.40", 1)
    with pytest.raises(ValueError, match="No positive running time"):
        attach_timings(tables, broken, "test")


def test_closed_stations_are_traversable_without_boarding(reference):
    tables = build_tables(reference[0], NOW.date())
    closed = {"metro:mop-frn", "metro:mop-pap"}
    calls = [r for r in tables["stop_times.txt"] if r["stop_id"] in closed]
    assert len(calls) == 12  # Two stations, two directions, three day types.
    assert all(r["pickup_type"] == r["drop_off_type"] == 1 for r in calls)
    assert all(r["from_stop_id"] not in closed and r["to_stop_id"] not in closed for r in tables["transfers.txt"])


def test_gtfs_relations_frequencies_and_monotonic_offsets(reference):
    tables = build_tables(reference[0], NOW.date())
    assert len(tables["routes.txt"]) == 6
    assert len(tables["trips.txt"]) == 36
    stops = {r["stop_id"] for r in tables["stops.txt"]}
    trips = {r["trip_id"] for r in tables["trips.txt"]}
    services = {r["service_id"] for r in tables["calendar.txt"]}
    assert all(r["service_id"] in services for r in tables["trips.txt"])
    assert all(r["trip_id"] in trips and r["stop_id"] in stops for r in tables["stop_times.txt"])
    for trip in trips:
        calls = [r for r in tables["stop_times.txt"] if r["trip_id"] == trip]
        assert calls[0]["departure_time"] == "00:00:00"
        assert all(a["departure_time"] < b["arrival_time"] for a, b in zip(calls, calls[1:]))
        freq = sorted((r for r in tables["frequencies.txt"] if r["trip_id"] == trip), key=lambda r:r["start_time"])
        assert freq and all(r["exact_times"] == 0 and r["headway_secs"] > 0 and r["start_time"] < r["end_time"] for r in freq)
        assert all(a["end_time"] <= b["start_time"] for a, b in zip(freq, freq[1:]))
    first = next(r for r in tables["frequencies.txt"] if r["trip_id"] == "metro:1:0:weekday")
    assert first["headway_secs"] == 293  # rounded midpoint of 105..480, not 105 or waiting time.
    # Transfer times are walking only; headway waiting is already represented by frequencies.
    transfer = next(r for r in tables["transfers.txt"] if r["from_stop_id"] == "metro:kiv-psh")
    assert transfer["min_transfer_time"] == 140


def test_terminal_first_last_bounds(reference):
    tables = build_tables(reference[0], NOW.date())
    freqs = [r for r in tables["frequencies.txt"] if r["trip_id"] == "metro:2:0:weekday"]
    # Official line 2 ordering is Parnas -> Kupchino. Start cannot precede its first train.
    hours = reference[0]["station_hours.csv"]
    row = next(r for r in hours if r["station_id"] == "metro:mop-par")
    assert freqs[0]["start_time"] >= row["first_to_second_terminus_odd"].zfill(5) + ":00"
    assert all(r["end_time"] <= "24:00:00" for r in freqs)


@pytest.mark.parametrize("kind", ["missing_hours", "overlap", "bad_headway", "bad_edge"])
def test_invalid_source_preserves_validation_boundary(reference, kind):
    tables = copy.deepcopy(reference[0])
    if kind == "missing_hours":
        tables["station_hours.csv"] = []
    elif kind == "overlap":
        tables["intervals.csv"].append(dict(tables["intervals.csv"][0]))
    elif kind == "bad_headway":
        tables["intervals.csv"][0]["min_headway_seconds"] = -1
    else:
        tables["segments.csv"][0]["duration_seconds"] = 0
    with pytest.raises(ValueError):
        build_tables(tables, NOW.date())


def test_gtfs_zip_is_deterministic_and_inspectable(tmp_path, reference):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    for path in (a, b):
        write_feed(path, reference[0], NOW.date(), reference[1])
    assert a.read_bytes() == b.read_bytes()
    details = inspect_archive(a, "20261111")
    assert details["active_service_ids"] == 1 and details["invalid_calendar_rows"] == 0
    with zipfile.ZipFile(a) as archive:
        provenance = json.loads(archive.read("metro_reference.json"))
        assert provenance["warnings"] and provenance["sources"]
        frequencies = list(csv.DictReader(io.StringIO(archive.read("frequencies.txt").decode())))
        assert {r["exact_times"] for r in frequencies} == {"0"}


def test_refresh_unchanged_prunes_and_failure_keeps_previous(tmp_path, reference):
    result = refresh(tmp_path, load=lambda: reference, now=NOW)
    assert result["status"] == "updated" and result["usable"]
    current = (tmp_path / "current.json").read_bytes()
    stale = tmp_path / "versions" / ("a" * 64)
    stale.mkdir(); (stale / "feed.zip").write_bytes(b"old")
    assert refresh(tmp_path, load=lambda: reference, now=NOW)["status"] == "unchanged"
    assert not stale.exists()
    broken = copy.deepcopy(reference)
    broken[0]["segments.csv"][0]["duration_seconds"] = 0
    with pytest.raises(ValueError):
        refresh(tmp_path, load=lambda: broken, now=NOW)
    assert (tmp_path / "current.json").read_bytes() == current
    status = read_json(tmp_path / "refresh-status.json")
    assert status["status"] == "error" and status["last_success_at"] == NOW.isoformat()
    assert not list(tmp_path.glob(".metro-*"))


def test_refresh_lock(tmp_path):
    with store_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            refresh(tmp_path, load=lambda: pytest.fail("Must not fetch"), now=NOW)


def test_failed_first_fetch_does_not_publish_or_log_credentials(tmp_path):
    def fail():
        raise ValueError("http://user:secret@proxy.invalid")
    with pytest.raises(ValueError):
        refresh(tmp_path, load=fail, now=NOW)
    assert not (tmp_path / "current.json").exists()
    assert "secret" not in (tmp_path / "refresh-status.json").read_text()


def test_fetch_uses_pinned_pmetro_revision_and_tls(reference):
    from data.metro import refresh as module
    from unittest.mock import patch
    from data.metro import official_reference as official
    revision = "a" * 40
    payloads = {official.INTERVAL_URL: b"intervals", official.HOURS_URL: b"hours",
                official.MAP_URL: b"map", official.GRAPH_URL: b"graph", official.COORD_URL: b'{}',
                module.PMETRO_REPO: json.dumps({"sha": revision}).encode(),
                f"{module.PMETRO_BASE}/{revision}/maps/Peterburg/Metro.trp": FIXTURE.read_text().encode("cp1251")}
    calls = []

    class Response:
        def __init__(self, content): self.content = content
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size): yield self.content

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(payloads[url])

    tables = reference[0]
    with patch.object(official, "extract_station_hours", return_value=tables["station_hours.csv"]), \
         patch.object(official, "extract_network", return_value=(tables["stations.csv"], tables["segments.csv"], tables["transfers.csv"])), \
         patch.object(official, "attach_station_ids"), \
         patch.object(official, "extract_intervals", return_value=tables["intervals.csv"]):
        result, sources = fetch_sources(get=get)
    assert len(result["segments.csv"]) == 69
    assert sources[-1]["git_commit"] == revision
    assert all(kwargs["verify"] is not False for _, kwargs in calls)
    assert calls[-1][0].endswith(f"/{revision}/maps/Peterburg/Metro.trp")
    assert len(calls) == 7


def test_download_rejects_oversize():
    from data.metro.refresh import download, MAX_SOURCE_BYTES
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size):
            yield b"x" * MAX_SOURCE_BYTES
            yield b"x"
    with pytest.raises(ValueError, match="exceeds"):
        download("https://example.com", get=lambda *a, **kw: Response())


def test_refresh_uses_completion_day_across_midnight(tmp_path, reference):
    from unittest.mock import patch
    before = datetime(2026, 11, 11, 23, 59, 59, tzinfo=NOW.tzinfo)
    after = datetime(2026, 11, 12, 0, 0, 1, tzinfo=NOW.tzinfo)
    with patch("data.metro.refresh.datetime") as clock:
        clock.now.side_effect = [before, after]
        result = refresh(tmp_path, load=lambda: reference)
    assert result["service_date"] == "20261112"
    assert result["attempted_at"] == before.isoformat()
    assert result["checked_at"] == after.isoformat()
