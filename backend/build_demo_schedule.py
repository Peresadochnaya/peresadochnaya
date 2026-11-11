"""Give the bundled demo two explicitly synthetic, all-day 10-minute services."""

import csv
import io
import json
from pathlib import Path
import zipfile


def main():
    root = Path(__file__).parent / "app" / "fixtures" / "gtfs"
    path = root / "demo.zip"
    with zipfile.ZipFile(path) as archive:
        tables = {name: archive.read(name) for name in archive.namelist()}
    trips = list(csv.DictReader(io.StringIO(tables["trips.txt"].decode("utf-8-sig"))))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["trip_id", "start_time", "end_time", "headway_secs", "exact_times"], lineterminator="\n")
    writer.writeheader()
    for trip in trips:
        writer.writerow(dict(trip_id=trip["trip_id"], start_time="00:00:00", end_time="24:00:00", headway_secs=600, exact_times=1))
    tables["frequencies.txt"] = stream.getvalue().encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in tables.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    manifest = json.loads((root / "provenance.json").read_text())
    manifest["note"] = "Real SPb stops/routes/trip patterns/shapes; synthetic all-week calendar 2000-2099 and exact 10-minute departure grids, all day in both directions. Not real service availability."
    (root / "provenance.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
