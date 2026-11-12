#!/usr/bin/env bash
# ============================================================================
# Entrypoint для контейнера OpenTripPlanner
# ============================================================================
# Запускает OTP той же командой, что и /docker-entrypoint.sh официального
# образа, но с базовым каталогом из OTP_HOMEDIR:
#   java $JAVA_OPTS -cp @/app/jib-classpath-file @/app/jib-main-class-file \
#        "$OTP_HOMEDIR/" $@
#
# Режимы (первый аргумент или OTP_MODE):
#   build — собрать graph.obj из OSM/GTFS в OTP_HOMEDIR и завершиться
#   serve — загрузить graph.obj и запустить сервер маршрутизации
#
# Версия графа — отдельный самодостаточный каталог (graph.obj + три JSON).
# В режиме serve в OTP_HOMEDIR лежит только активная версия (обычно через
# симлинк data/current), каталог смонтирован для чтения, поэтому автоматическая
# сборка при отсутствии графа не выполняется: сначала scripts/build-graph.sh.
# Симлинк разрешается внутри контейнера, а не при монтировании: Docker Desktop
# запоминает цель симлинка на момент создания контейнера.
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
MODE="${1:-${OTP_MODE:-serve}}"
PORT="${OTP_PORT:-8080}"

# ----------------------------------------------------------------------------
# Запуск OTP с явным базовым каталогом.
# Официальный /docker-entrypoint.sh жёстко подставляет /var/opentripplanner/,
# поэтому путь передаём сами. Это нужно, когда активная версия графа выбрана
# симлинком: Docker Desktop резолвит симлинк при монтировании один раз и потом
# отдаёт старую цель, а путь, разрешённый внутри контейнера, всегда актуален.
# ----------------------------------------------------------------------------
run_otp() {
  # shellcheck disable=SC2086  # JAVA_OPTS должен разбиваться на аргументы
  exec java ${JAVA_OPTS} -cp @/app/jib-classpath-file @/app/jib-main-class-file "${WORKDIR}/" "$@"
}

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
check_inputs() {
  local osm gtfs metro
  osm="$(find "${WORKDIR}" -maxdepth 1 -name '*.osm.pbf' 2>/dev/null | head -1 || true)"
  # OTP находит фиды по шаблону (?i)gtfs в имени файла, а не по содержимому.
  gtfs="$(find "${WORKDIR}" -maxdepth 1 -iname '*gtfs*.zip' 2>/dev/null | head -1 || true)"
  metro="$(find "${WORKDIR}" -maxdepth 1 -name 'metro.gtfs.zip' 2>/dev/null | head -1 || true)"
  [ -n "${osm}" ]   && log "OSM:   $(basename "${osm}")"
  [ -n "${gtfs}" ]  && log "GTFS:  $(basename "${gtfs}")"
  [ -n "${metro}" ] && log "Метро: $(basename "${metro}")"
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
  actual="$(run_otp --version 2>/dev/null \
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
  run_otp --build --save --abortOnUnknownConfig
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
  run_otp --load --port "${PORT}" --abortOnUnknownConfig
}

case "${MODE}" in
  build)
    mkdir -p "${WORKDIR}"
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
