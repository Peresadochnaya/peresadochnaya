"""Тесты исправления календаря GTFS; файлы только временные, сеть не нужна."""
import importlib.util
import zipfile
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "fix-gtfs-calendar-dates.py"
spec = importlib.util.spec_from_file_location("fix_calendar", MODULE_PATH)
fix_calendar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix_calendar)

HEADER = ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
          "start_date,end_date,service_name")


def feed(path: Path, row: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("calendar.txt", f"{HEADER}\n{row}\n")
        archive.writestr("calendar_dates.txt", "service_id,date,exception_type\n1,20261104,1\n")
        archive.writestr("agency.txt", "agency_id,agency_name\n1,ОРГП\n")
    return path


def test_inverted_end_date_with_trailing_column(tmp_path):
    # service_name намеренно ASCII: скрипт работает в latin-1 побайтово,
    # кириллица в UTF-8 выглядела бы как другой текст после декодирования.
    path = feed(tmp_path / "feed.zip", "1,1,1,1,1,1,0,0,20260801,20191230,Weekdays")
    raw = zipfile.ZipFile(path).read("calendar.txt")
    fixed, fixed_count, skipped, records = fix_calendar.fix_calendar(raw, "20291109")
    assert (fixed_count, skipped, records) == (1, 0, 1)
    text = fixed.decode("latin-1")
    assert "20260801,20291109,Weekdays" in text
    assert fix_calendar.count_inversions(fixed) == 0


def test_inverted_end_date_is_last_column(tmp_path):
    path = feed(tmp_path / "feed.zip", "1,1,1,1,1,1,0,0,20260801,20191230")
    raw = zipfile.ZipFile(path).read("calendar.txt")
    fixed, fixed_count, skipped, records = fix_calendar.fix_calendar(raw, "20291109")
    assert (fixed_count, skipped, records) == (1, 0, 1)
    assert fixed.decode("latin-1").split("\n")[1] == "1,1,1,1,1,1,0,0,20260801,20291109"


def test_valid_ranges_are_untouched(tmp_path):
    path = feed(tmp_path / "feed.zip", "1,1,1,1,1,1,0,0,20260801,20291109,Weekdays")
    raw = zipfile.ZipFile(path).read("calendar.txt")
    fixed, fixed_count, _, records = fix_calendar.fix_calendar(raw, "20300101")
    assert (fixed_count, records) == (0, 1)
    assert fixed == raw


def test_other_files_are_preserved_byte_for_byte(tmp_path):
    path = feed(tmp_path / "feed.zip", "1,1,1,1,1,1,0,0,20260801,20191230,Weekdays")
    before = zipfile.ZipFile(path).read("agency.txt")
    raw = zipfile.ZipFile(path).read("calendar.txt")
    fixed, _, _, _ = fix_calendar.fix_calendar(raw, "20291109")
    fix_calendar.rewrite_zip(str(path), fixed)
    with zipfile.ZipFile(path) as archive:
        assert archive.read("agency.txt") == before
        assert fix_calendar.count_inversions(archive.read("calendar.txt")) == 0


def test_max_date_comes_from_calendar_and_exceptions(tmp_path):
    path = tmp_path / "feed.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("calendar.txt", f"{HEADER}\n1,1,1,1,1,1,0,0,20260801,20291109,Weekdays\n")
        archive.writestr("calendar_dates.txt", "service_id,date,exception_type\n1,20300115,1\n")
    with zipfile.ZipFile(path) as archive:
        assert fix_calendar.find_max_date(archive).isoformat() == "2030-01-15"
