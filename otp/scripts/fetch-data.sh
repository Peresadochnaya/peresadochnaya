#!/usr/bin/env bash
# ============================================================================
# Скачать данные для OTP в каталог ./data
# ============================================================================
# Использование:
#   ./scripts/fetch-data.sh europe/russia/saint-petersburg-latest.osm.pbf
#   ./scripts/fetch-data.sh https://download.geofabrik.de/europe/germany-latest.osm.pbf
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DATA_DIR="${PROJECT_DIR}/data"
OSM_URL="${1:-}"

if [ -z "${OSM_URL}" ]; then
  echo "Использование: $0 <OSM_URL>"
  echo ""
  echo "Примеры:"
  echo "  $0 europe/russia/saint-petersburg-latest.osm.pbf"
  echo "  $0 https://download.geofabrik.de/europe/russia/saint-petersburg-latest.osm.pbf"
  exit 1
fi

# Преобразуем относительный URL Geofabrik в полный
if [[ "${OSM_URL}" == http* ]]; then
  URL="${OSM_URL}"
else
  URL="https://download.geofabrik.de/${OSM_URL}"
fi

FILENAME="$(basename "${URL}")"
TARGET="${DATA_DIR}/${FILENAME}"

mkdir -p "${DATA_DIR}"

if [ -f "${TARGET}" ]; then
  echo "Файл уже существует: ${TARGET}"
  echo "Пропускаю. Удалите для повторной загрузки."
else
  echo "Скачивание: ${URL}"
  echo "Цель: ${TARGET}"
  curl -L --progress-bar -o "${TARGET}" "${URL}"
  echo ""
  SIZE="$(du -h "${TARGET}" | cut -f1)"
  echo "Скачано: ${SIZE}"
fi
