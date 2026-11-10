import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from data.gtfs import refresh_static
from data.gtfs.refresh_static import activate_archive, inspect_archive


def make_feed(path, stop_count):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("agency.txt", "agency_id,agency_name,agency_url,agency_timezone\norgp,Test,https://example.com,Europe/Moscow\n")
        archive.writestr("routes.txt", "route_id,agency_id,route_short_name,route_type\n1087,orgp,31,3\n")
        archive.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(f"{i},Stop {i},60,30\n" for i in range(stop_count)))
        archive.writestr("trips.txt", "route_id,service_id,trip_id\n1087,work,trip-1\n")
        archive.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\ntrip-1,12:00:00,12:00:00,0,1\n")
        archive.writestr("calendar.txt", "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nwork,1,1,1,1,1,1,1,20261110,20191230\n")
        archive.writestr("calendar_dates.txt", "service_id,date,exception_type\n")


class RefreshTests(unittest.TestCase):
    def test_prepare_csv_extracts_every_table_with_matching_source_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory) / "feed.zip"
            csv_dir = Path(directory) / "processed" / "gtfs"
            make_feed(feed, 2)

            result = refresh_static.prepare_csv(feed, csv_dir)

            self.assertEqual(result["csv_status"], "updated")
            self.assertEqual(sorted(path.name for path in csv_dir.glob("*.csv")), [
                "agency.csv", "calendar.csv", "calendar_dates.csv", "routes.csv",
                "stop_times.csv", "stops.csv", "trips.csv",
            ])
            self.assertEqual(
                (csv_dir / "routes.csv").read_bytes(),
                b"route_id,agency_id,route_short_name,route_type\n1087,orgp,31,3\n",
            )
            manifest = json.loads((csv_dir / "manifest.json").read_text())
            self.assertEqual(manifest["source_sha256"], hashlib.sha256(feed.read_bytes()).hexdigest())

    def test_unchanged_archive_creates_missing_csv_files(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "portal-gtfs.zip"
            incoming = Path(directory) / "incoming.zip"
            csv_dir = Path(directory) / "processed" / "gtfs"
            make_feed(current, 1)
            shutil.copy2(current, incoming)

            result = activate_archive(incoming, current, "20261110", csv_dir=csv_dir)

            self.assertEqual(result["status"], "unchanged")
            self.assertEqual(result["csv_status"], "updated")
            self.assertIn("0,Stop 0,60,30", (csv_dir / "stops.csv").read_text())

            result = activate_archive(incoming, current, "20261110", csv_dir=csv_dir)
            self.assertEqual(result["csv_status"], "unchanged")

    def test_invalid_calendar_is_reported_without_discarding_valid_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory) / "feed.zip"
            make_feed(feed, 2)

            result = inspect_archive(feed, "20261110")

            self.assertEqual(result["routes"], 1)
            self.assertEqual(result["stops"], 2)
            self.assertEqual(result["invalid_calendar_rows"], 1)
            self.assertEqual(result["active_service_ids"], 0)

    def test_changed_archive_replaces_current_and_keeps_previous_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "portal-gtfs.zip"
            incoming = Path(directory) / "incoming.zip"
            make_feed(current, 1)
            make_feed(incoming, 2)

            csv_dir = Path(directory) / "processed" / "gtfs"
            refresh_static.prepare_csv(current, csv_dir)

            result = activate_archive(incoming, current, "20261110", csv_dir=csv_dir)

            self.assertEqual(result["status"], "updated")
            self.assertEqual(result["csv_status"], "updated")
            self.assertIn("1,Stop 1,60,30", (csv_dir / "stops.csv").read_text())
            self.assertEqual(inspect_archive(current, "20261110")["stops"], 2)
            self.assertEqual(inspect_archive(Path(directory) / "portal-gtfs.previous.zip", "20261110")["stops"], 1)


if __name__ == "__main__":
    unittest.main()
