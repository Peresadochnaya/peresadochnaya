#!/usr/bin/env bash
# ============================================================================
# Entrypoint для контейнера OpenTripPlanner
# ============================================================================
# Оборачивает /docker-entrypoint.sh официального образа:
#   java $JAVA_OPTS -cp @/app/jib-classpath-file @/app/jib-main-class-file \
#        /var/opentripplanner/ $@
#
# Режимы (первый аргумент или OTP_MODE):
#   build — собрать graph.obj из OSM/GTFS в /var/opentripplanner и завершиться
#   serve — загрузить graph.obj и запустить сервер маршрутизации
#
# Версия графа — отдельный самодостаточный каталог (graph.obj + три JSON).
# В режиме serve монтируется как /var/opentripplanner только для чтения,
# поэтому автоматическая сборка при отсутствии графа не выполняется:
# сначала запустите scripts/build-graph.sh.
#
# --abortOnUnknownConfig обязателен: опечатка в конфиге должна ломать запуск,
# а не тихо игнорироваться (как было с routingDefaults.maxWalkDistance).
#
# В режиме serve сверяется otpSerializationVersionId образа с manifest.json
# опубликованного графа: несовместимый graph.obj не загружается. Проверку
# можно отключить переменной OTP_SKIP_VERSION_CHECK=1.
# ============================================================================
set -euo pipefail

WORKDIR="${OTP_HOMEDIR:-/var/opentripplanner}"
CONFIG_SRC="${OTP_CONFIG_DIR:-/otp/config}"
MODE="${1:-${OTP_MODE:-serve}}"
PORT="${OTP_PORT:-8080}"

log() { printf '[otp-entrypoint] %s\n' "$*"; }

if [ -z "${JAVA_OPTS:-}" ]; then
  export JAVA_OPTS="-Xmx${OTP_HEAP_SIZE:-4G}"
  log "JAVA_OPTS по умолчанию: ${JAVA_OPTS}"
else
  log "JAVA_OPTS из окружения: ${JAVA_OPTS}"
fi

# ----------------------------------------------------------------------------
# Конфиги из образа нужны только для разовой сборки без подготовленного
# staging. Подготовленный staging всегда содержит свои конфиги с версией.
# ----------------------------------------------------------------------------
sync_config() {
  [ -d "${CONFIG_SRC}" ] || return 0
  for f in "${CONFIG_SRC}"/*.json; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    if [ -f "${WORKDIR}/${base}" ]; then
      log "конфиг уже на месте, не трогаю: ${base}"
    else
      cp "$f" "${WORKDIR}/${base}"
      log "конфиг установлен из образа: ${base}"
    fi
  done
}

check_inputs() {
  local osm gtfs
  osm="$(find "${WORKDIR}" -maxdepth 1 -name '*.osm.pbf' 2>/dev/null | head -1 || true)"
  gtfs="$(find "${WORKDIR}" -maxdepth 1 \( -iname '*gtfs*.zip' -o -name 'feed.zip' \) 2>/dev/null | head -1 || true)"
  [ -n "${osm}" ]  && log "OSM:  $(basename "${osm}")"
  [ -n "${gtfs}" ] && log "GTFS: $(basename "${gtfs}")"
  if [ -z "${gtfs}" ]; then
    log "ERROR: в ${WORKDIR} нет GTFS ZIP — расписание не попадёт в граф."
    exit 1
  fi
  if [ -z "${osm}" ]; then
    log "WARN: в ${WORKDIR} нет *.osm.pbf — граф будет без улиц и пеших путей."
  fi
}

# ----------------------------------------------------------------------------
# Совместимость графа с образом.
# graph.obj — Java-сериализация с внутренним otpSerializationVersionId.
# Несовпадение версий даёт непонятную ошибку загрузки, поэтому проверяем
# заранее и подсказываем, что делать.
# ----------------------------------------------------------------------------
check_graph_version() {
  local manifest="${WORKDIR}/manifest.json" expected actual
  if [ "${OTP_SKIP_VERSION_CHECK:-0}" = "1" ]; then
    log "проверка совместимости графа отключена (OTP_SKIP_VERSION_CHECK=1)"
    return 0
  fi
  if [ ! -f "${manifest}" ]; then
    log "WARN: ${manifest} не найден — версия сборки графа неизвестна, проверка пропущена"
    return 0
  fi
  expected="$(sed -n 's/.*"otp_serialization_version_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "${manifest}" | head -1)"
  if [ -z "${expected}" ]; then
    log "WARN: в manifest.json нет otp_serialization_version_id, проверка пропущена"
    return 0
  fi
  actual="$(/docker-entrypoint.sh --version 2>/dev/null \
    | sed -n 's/.*ser\.ver\.id:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1 || true)"
  if [ -z "${actual}" ]; then
    log "WARN: не удалось определить ser.ver.id образа, проверка совместимости пропущена"
    return 0
  fi
  if [ "${actual}" != "${expected}" ]; then
    log "ERROR: graph.obj собран OTP с ser.ver.id=${expected}, а образ имеет ${actual}."
    log "OTP не умеет читать граф другой версии. Пересоберите граф: ./scripts/build-graph.sh --force"
    log "Если расхождение ожидаемо, отключите проверку: OTP_SKIP_VERSION_CHECK=1"
    exit 1
  fi
  log "граф совместим с образом: ser.ver.id=${actual}"
}

build_graph() {
  log "=== сборка графа (несколько минут) ==="
  /docker-entrypoint.sh --build --save --abortOnUnknownConfig
  if [ -f "${WORKDIR}/graph.obj" ]; then
    log "граф сохранён: $(du -h "${WORKDIR}/graph.obj" | cut -f1)"
  else
    log "ERROR: сборка завершилась, но graph.obj не найден"
    exit 1
  fi
}

serve_graph() {
  if [ ! -f "${WORKDIR}/graph.obj" ]; then
    log "ERROR: graph.obj не найден в ${WORKDIR}."
    log "Соберите и опубликуйте граф: ./scripts/build-graph.sh"
    log "В режиме serve каталог смонтирован только для чтения, сборка не выполняется."
    exit 1
  fi
  check_graph_version
  log "=== запуск OTP-сервера на порту ${PORT} ==="
  log "API:      http://localhost:${PORT}/otp/gtfs/v1"
  log "GraphiQL: http://localhost:${PORT}/graphiql"
  # --load подразумевает --serve.
  # ВАЖНО: у флага --port синтаксис только через пробел ("--port 8080").
  # Запись "--port=8080" OTP принимает как позиционный аргумент-директорию.
  exec /docker-entrypoint.sh --load --port "${PORT}" --abortOnUnknownConfig
}

case "${MODE}" in
  build)
    mkdir -p "${WORKDIR}"
    sync_config
    check_inputs
    build_graph
    ;;
  serve)
    serve_graph
    ;;
  *)
    log "неизвестный режим: ${MODE} (build | serve)"
    exit 1
    ;;
esac
