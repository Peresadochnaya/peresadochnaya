# ============================================================================
# OpenTripPlanner 2.x — образ для построения маршрутов на карте
# ============================================================================
# Модуль OTP (OpenTripPlanner) предоставляет GraphQL API для multi-modal
# планирования маршрутов с использованием OSM-данных и GTFS-расписаний.
#
# Базовая версия запинена по digest: graph.obj совместим только с той сборкой
# OTP, которая его создала (otpSerializationVersionId). Digest соответствует
# образу, на котором собран текущий граф (ser.ver.id=289), поэтому пересборка
# не требуется. При обновлении digest пересоберите граф: scripts/build-graph.sh.
#
#   digest:        sha256:11289dd093bae3845c2d2355a1845069de83e2809c14a0949dceb20a62359763
#   версия:        2.11.0-SNAPSHOT, ser.ver.id: 289
#   commit:        f64e860dae6a426a552570673033ca1db512d567 (dev-2.x)
#   проверено:     2026-11-11
#
# entrypoint в режиме serve сверяет ser.ver.id образа с manifest.json графа
# и падает при расхождении, а не пытается загрузить несовместимый graph.obj.
# ============================================================================

FROM opentripplanner/opentripplanner@sha256:11289dd093bae3845c2d2355a1845069de83e2809c14a0949dceb20a62359763

LABEL maintainer="hackathon-team"
LABEL description="OpenTripPlanner (OTP) — сервис маршрутизации для карты"
LABEL version="2.11"

# ---------------------------------------------------------------------------
# Переменные окружения
# OTP_MODE=build  → собрать граф и завершиться
# OTP_MODE=serve  → запустить сервер маршрутизации (по умолчанию)
# JAVA_OPTS       → параметры JVM (по умолчанию: -Xmx4G)
# OTP_PORT        → порт сервера (по умолчанию: 8080)
# ---------------------------------------------------------------------------
ENV OTP_MODE=serve
ENV OTP_PORT=8080

COPY config/ /otp/config/
COPY scripts/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]

