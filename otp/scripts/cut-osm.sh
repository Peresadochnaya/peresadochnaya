#!/usr/bin/env bash
# ============================================================================
# Вырезать из большого OSM-файла только нужную область (bbox)
# ============================================================================
# Geofabrik отдаёт Санкт-Петербург только внутри northwestern-fed-district
# (~657 МБ на весь СЗФО). Для OTP это избыточно: GTFS ОРГП покрывает только
# СПб и Ленобласть, а лишний регион замедляет сборку графа в разы.
#
# Использование:
#   ./scripts/cut-osm.sh <INPUT.osm.pbf> <OUTPUT.osm.pbf> [BBOX]
#   BBOX: minlon,minlat,maxlon,maxlat (по умолчанию — СПб + ЛО)
#
# Требуется Docker (образ stefda/osmium-tool).
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$(cd "${SCRIPT_DIR}/../data" && pwd)"

INPUT="${1:-}"
OUTPUT="${2:-}"
# СПб + Ленинградская область по границам GTFS-остановок + буфер ~5 км
BBOX="${3:-28.17,59.32,33.59,61.09}"

if [ -z "${INPUT}" ] || [ -z "${OUTPUT}" ]; then
  echo "Использование: $0 <INPUT.osm.pbf> <OUTPUT.osm.pbf> [BBOX]"
  echo "  BBOX = minlon,minlat,maxlon,maxlat"
  echo "  По умолчанию: ${BBOX} (СПб + ЛО)"
  echo ""
  echo "Пример:"
  echo "  $0 data/northwestern-fed-district-261108.osm.pbf data/spb.osm.pbf"
  exit 1
fi

# Нормализуем относительные пути от корня модуля
resolve() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *)  printf '%s' "$(cd "${SCRIPT_DIR}/.." && pwd)/$1" ;;
  esac
}
INPUT_ABS="$(resolve "${INPUT}")"
OUTPUT_ABS="$(resolve "${OUTPUT}")"

if [ ! -f "${INPUT_ABS}" ]; then
  echo "ERROR: входной файл не найден: ${INPUT_ABS}"
  exit 1
fi
# В контейнер монтируется только ./data, поэтому оба файла должны лежать в нём
for p in "${INPUT_ABS}" "${OUTPUT_ABS}"; do
  case "${p}" in
    "${DATA_DIR}"/*) ;;
    *) echo "ERROR: файл должен лежать в ${DATA_DIR}: ${p}"; exit 1 ;;
  esac
done
if [ -f "${OUTPUT_ABS}" ]; then
  echo "Выходной файл уже существует: ${OUTPUT_ABS}"
  echo "Удалите его, чтобы перезаписать."
  exit 1
fi

IN_SIZE="$(du -h "${INPUT_ABS}" | cut -f1)"
echo "Вход:   ${INPUT_ABS} (${IN_SIZE})"
echo "Выход:  ${OUTPUT_ABS}"
echo "bbox:   ${BBOX}"
echo ""
echo "Стратегия complete_ways сохраняет связность дорог на границах вырезки"
echo "(важно для маршрутизации OTP: обрывы на краю bbox дают недостижимые участки)."
echo ""

docker run --rm \
  -v "${DATA_DIR}:/data" \
  stefda/osmium-tool:latest \
  osmium extract \
    --strategy=complete_ways \
    --bbox="${BBOX}" \
    --overwrite \
    -o "/data/$(basename "${OUTPUT_ABS}")" \
    "/data/$(basename "${INPUT_ABS}")"

echo ""
OUT_SIZE="$(du -h "${OUTPUT_ABS}" | cut -f1)"
echo "Готово: ${OUTPUT_ABS} (${OUT_SIZE})"
