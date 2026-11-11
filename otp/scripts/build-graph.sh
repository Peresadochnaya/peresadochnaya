#!/usr/bin/env bash
# ============================================================================
# Собрать и опубликовать новую версию графа OTP
# ============================================================================
# Один запуск делает три шага:
#   1. prepare — копирует GTFS в staging, исправляет календарь копии,
#      вычисляет версию по (GTFS + OSM + конфиги + версия OTP);
#   2. build   — собирает graph.obj контейнером otp-build;
#   3. publish — атомарно переносит staging в versions/<version> и
#      переключает указатели current и current.json.
#
# Если версия уже опубликована, сборка пропускается (кроме --force).
#
# Использование:
#   ./scripts/build-graph.sh                       # GTFS из ../data/gtfs
#   ./scripts/build-graph.sh --gtfs-zip data/incoming/portal-gtfs.zip
#   ./scripts/build-graph.sh --restart             # + пересоздать контейнер otp
#   ./scripts/build-graph.sh --force               # пересобрать ту же версию
#
# Переменные: OTP_DATA_DIR, OSM_PBF, GTFS_STORE, GTFS_ZIP, METRO_STORE,
#             METRO_ZIP, OTP_IMAGE.
# METRO_STORE="" отключает метро; по умолчанию берётся ../data/metro.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OTP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT_DIR="$(cd "${OTP_DIR}/.." && pwd)"

DATA_DIR="${OTP_DATA_DIR:-${OTP_DIR}/data}"
OSM_PBF="${OSM_PBF:-${DATA_DIR}/osm/spb-lo.osm.pbf}"
GTFS_STORE="${GTFS_STORE:-${ROOT_DIR}/data/gtfs}"
GTFS_ZIP="${GTFS_ZIP:-}"
METRO_STORE="${METRO_STORE-${ROOT_DIR}/data/metro}"
METRO_ZIP="${METRO_ZIP:-}"
OTP_IMAGE="${OTP_IMAGE:-peresadochnaya/otp:local}"
RESTART=0
FORCE=0

log() { printf '[build-graph] %s\n' "$*"; }

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --gtfs-zip) GTFS_ZIP="${2:-}"; shift 2 ;;
    --gtfs-store) GTFS_STORE="${2:-}"; shift 2 ;;
    --osm-pbf) OSM_PBF="${2:-}"; shift 2 ;;
    --data-dir) DATA_DIR="${2:-}"; shift 2 ;;
    --restart) RESTART=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: неизвестный аргумент: $1" >&2; usage; exit 1 ;;
  esac
done

if [ ! -f "${OSM_PBF}" ]; then
  log "ERROR: OSM PBF не найден: ${OSM_PBF}"
  log "Файл не хранится в Git. Положите готовый spb-lo.osm.pbf в ${DATA_DIR}/osm/"
  log "или соберите его один раз скриптом cut-osm.sh."
  exit 1
fi

if ! docker image inspect "${OTP_IMAGE}" >/dev/null 2>&1; then
  log "образ ${OTP_IMAGE} не найден — собираю"
  docker compose -f "${OTP_DIR}/docker-compose.yml" build otp-build
fi

# Версия и serialization id берутся из самого образа: граф совместим только
# с той версией OTP, которая его создала.
VERSION_LINE="$(docker run --rm --entrypoint /docker-entrypoint.sh "${OTP_IMAGE}" --version 2>/dev/null | head -1)"
OTP_VERSION="$(printf '%s' "${VERSION_LINE}" | sed -n 's/.*Version: \([^,]*\),.*/\1/p')"
OTP_SERIAL="$(printf '%s' "${VERSION_LINE}" | sed -n 's/.*ser\.ver\.id: \([^,]*\),.*/\1/p')"
if [ -z "${OTP_VERSION}" ] || [ -z "${OTP_SERIAL}" ]; then
  log "ERROR: не удалось определить версию OTP из образа: ${VERSION_LINE}"
  exit 1
fi
log "OTP ${OTP_VERSION} (serialization id ${OTP_SERIAL})"

PREPARE_ARGS=(prepare
  --data-dir "${DATA_DIR}"
  --config-dir "${OTP_DIR}/config"
  --osm-pbf "${OSM_PBF}"
  --otp-version "${OTP_VERSION}"
  --otp-serialization-version-id "${OTP_SERIAL}")
if [ -n "${GTFS_ZIP}" ]; then
  PREPARE_ARGS+=(--gtfs-zip "${GTFS_ZIP}")
else
  PREPARE_ARGS+=(--gtfs-store "${GTFS_STORE}")
fi
if [ -n "${METRO_ZIP}" ]; then
  PREPARE_ARGS+=(--metro-zip "${METRO_ZIP}")
elif [ -n "${METRO_STORE}" ]; then
  PREPARE_ARGS+=(--metro-store "${METRO_STORE}")
fi
[ "${FORCE}" -eq 1 ] && PREPARE_ARGS+=(--force)

PREPARED="$(python3 "${SCRIPT_DIR}/graph_store.py" "${PREPARE_ARGS[@]}")"
STATUS="$(printf '%s' "${PREPARED}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
VERSION="$(printf '%s' "${PREPARED}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"

if [ "${STATUS}" = "unchanged" ]; then
  log "версия ${VERSION} уже опубликована — сборка не нужна"
  log "пересоздать контейнер: docker compose up -d --force-recreate otp"
  exit 0
fi

log "версия ${VERSION}: входные данные подготовлены в ${DATA_DIR}/staging"
log "=== сборка графа (несколько минут) ==="

on_error() {
  local code=$?
  python3 "${SCRIPT_DIR}/graph_store.py" mark --data-dir "${DATA_DIR}" \
    --status error --error "build failed (exit ${code})" >/dev/null 2>&1 || true
  log "ERROR: сборка не завершена, активная версия графа не изменена"
  exit "${code}"
}
trap on_error ERR

docker compose -f "${OTP_DIR}/docker-compose.yml" run --rm otp-build

python3 "${SCRIPT_DIR}/graph_store.py" publish --data-dir "${DATA_DIR}"
trap - ERR
log "граф опубликован: ${DATA_DIR}/versions/${VERSION}"

if [ "${RESTART}" -eq 1 ]; then
  log "пересоздаю контейнер otp"
  docker compose -f "${OTP_DIR}/docker-compose.yml" up -d --force-recreate otp
  for _ in $(seq 1 60); do
    if python3 "${SCRIPT_DIR}/graph_store.py" verify --data-dir "${DATA_DIR}" --url "http://localhost:${OTP_PORT:-8080}" 2>/dev/null; then
      log "OTP загрузил версию ${VERSION}"
      exit 0
    fi
    sleep 2
  done
  log "ERROR: OTP не подтвердил загрузку версии ${VERSION}; смотрите docker compose logs otp"
  exit 1
fi

log "дальше: docker compose up -d --force-recreate otp"
log "затем:  python3 scripts/graph_store.py verify --url http://localhost:8080"
