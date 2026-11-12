#!/bin/sh
# Offline build; osmium==4.3.2 is the sole optional build dependency.
set -eu
cd "$(dirname "$0")/.."
: "${PYTHON:=.venv/bin/python}"
: "${OSM_PBF:=../spb-lo.osm.pbf}"
: "${GTFS_ZIP:=../data/raw/portal-gtfs.zip}"
: "${GTFS_INDEX_DIR:=/tmp/peresadochnaya-gtfs-index}"
export GTFS_INDEX_DIR
index_path="$($PYTHON - "$GTFS_ZIP" <<'PY'
from pathlib import Path
import hashlib,sys,zipfile
from app.gtfs_index import build,index_directory,valid,FORMAT
archive=Path(sys.argv[1]); digest=hashlib.file_digest(archive.open('rb'),'sha256').hexdigest()
folder=index_directory(); folder.mkdir(parents=True,exist_ok=True); path=folder/f'v{FORMAT}-{digest}.sqlite3'
if not valid(path,digest):
 with zipfile.ZipFile(archive) as source: build(source,path,digest)
print(path)
PY
)"
set --
if [ -f app/fixtures/traffic/official-resolved-lanes.geojson ]; then
    set -- --supplemental app/fixtures/traffic/official-resolved-lanes.geojson
fi
exec "$PYTHON" -m app.traffic_lane_builder --osm "$OSM_PBF" --index "$index_path" --output app/fixtures/traffic/road-status.json "$@"
