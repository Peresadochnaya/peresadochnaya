# Пересадочная

**Пересадочная** — веб-навигатор по общественному транспорту Санкт-Петербурга и
Ленинградской области. Пользователь задаёт точки A и B, выбирает время, виды
транспорта и проездной, а сервис показывает варианты маршрута на карте,
стоимость поездки и детализацию по каждому участку.

**Живой сервис:** <https://peresadochnaya.lol>

## Что умеет

- Строит маршруты по метро, автобусу, трамваю, троллейбусу, электричкам и
  пешим участкам. Планировщик — OpenTripPlanner 2.11.
- Показывает маршруты на Яндекс.Картах: точки A/B, геометрию, остановки и
  положения транспорта в реальном времени.
- Подсказывает адреса и умеет геокодировать/разгеокодировать точки через
  Яндекс.Геокодер и 2ГИС.
- Считает стоимость проезда по тарифам 2026 года: банковская карта,
  «Подорожник», безлимитные проездные. Учитывает пересадочный тариф, 
  а также резервный способ оплаты для безлимитных проездных только на один вид транспорта.
- Работает в двух режимах: `dummy=true` (по умолчанию) — согласованные
  демонстрационные данные без внешних запросов; `dummy=false` — реальные
  маршруты, геокодер и дорожные оценки.
- Отдаёт REST API с OpenAPI-контрактом (`/docs`, `/openapi.json`, корневой
  `openapi.yaml`).

## Архитектура

```text
Браузер
  │  React + Vite + TypeScript + Tailwind
  ▼
frontend  ──/api/v1──►  backend (FastAPI)
                          ├── OpenTripPlanner 2.11 (GraphQL) ── граф GTFS + OSM
                          ├── Яндекс.Геокодер / Suggest / Router
                          ├── 2ГИС
                          ├── realtime (GTFS-RT Vehicle Positions)
                          └── /data: GTFS наземного транспорта, метро, электричек
```

| Компонент | Каталог | Стек | Роль |
|---|---|---|---|
| Frontend | `frontend/` | React 18, Vite, TypeScript, Tailwind | Карта, поиск, фильтры, карточки маршрутов, профили проездных |
| Backend | `backend/` | FastAPI, Pydantic, httpx | REST API, адаптер OTP, тарифы, геокодер, дорожные поправки |
| OTP | `otp/` | OpenTripPlanner 2.11, Docker | Мультимодальная маршрутизация по GTFS и OSM |
| Данные | `data/` | Python | GTFS наземного транспорта, оценочный GTFS метро, GTFS электричек, GTFS-RT |
| Realtime | `realtime/` | Python + C++17 | Сбор позиций транспорта, расчёт TripUpdates при наличии привязки к рейсу |
| Развёртывание | `deploy/` | Kubernetes (k3s), Traefik, GHCR | Манифесты, cronjob-обновления, ingress |

## Требования

- Python 3.11+ (CI использует 3.12, Docker-образ — 3.13).
- Node.js 22+ и npm.
- Docker — для OTP и production-подобной сборки.
- Для сборки графа OTP: OSM PBF, GTFS-фиды и не менее ~6 ГБ RAM на сборку
  (сервер OTP работает при ~4 ГБ).

## Быстрый старт: демо без OTP

Самый короткий путь — поднять backend в демо-режиме и frontend в dev-сервере.

```bash
# из корня репозитория
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt -r data/requirements.txt

cd backend
PLANNER_MODE=dummy ../.venv/bin/uvicorn app.main:app --reload
```

Backend будет доступен на <http://localhost:8000>, Swagger — на
<http://localhost:8000/docs>.

Во втором терминале:

```bash
cd frontend
npm ci
npm run dev
```

Frontend откроется на <http://localhost:5173>. По умолчанию Vite проксирует
`/api` на <https://peresadochnaya.lol> (см. `frontend/vite.config.ts`). Чтобы
ходить в локальный backend, замените `target` на `http://localhost:8000`.

## Полный локальный стек с OTP

Нужны GTFS, OSM и собранный граф OTP. Подробности — в `data/README.md` и
`otp/README.md`.

### 1. GTFS наземного транспорта и метро

```bash
# из корня репозитория
.venv/bin/python -m data.gtfs.versioned_static --store-dir data/gtfs
.venv/bin/python -m data.metro.refresh --store-dir data/metro
```

### 2. OSM и граф OTP

OSM скачивается и вырезается один раз; это тяжёлая операция.

```bash
cd otp
./scripts/fetch-data.sh northwestern-fed-district-latest.osm.pbf
./scripts/cut-osm.sh data/northwestern-fed-district-latest.osm.pbf \
                    data/osm/spb-lo.osm.pbf
./scripts/build-graph.sh
docker compose up -d otp
```

OTP поднимется на <http://localhost:8080>. Проверка:
`curl -f http://localhost:8080/otp/actuators/health`.

> Локальный `build-graph.sh` собирает граф из наземного GTFS и метро. В k3s
> используется `prepare_combined.py`, который дополнительно включает в граф
> датированный GTFS электричек.

### 3. Backend с реальным планировщиком

Backend читает версии GTFS и графа из `DATA_ROOT`. Локально удобно собрать
каталог со ссылками на оба хранилища:

```bash
cd ../backend
DR=$(mktemp -d)
ln -s "$(cd .. && pwd)/data/gtfs" "$DR/gtfs"
ln -s "$(cd .. && pwd)/otp/data"  "$DR/otp"

PLANNER_MODE=otp OTP_URL=http://localhost:8080 DATA_ROOT="$DR" \
  ../.venv/bin/uvicorn app.main:app --reload
```

Frontend запускается так же, как в демо-режиме, но с прокси на
`http://localhost:8000`.

## Сборка production-подобного Docker-образа

Образ собирается из корня репозитория: `backend/Dockerfile` сначала собирает
frontend, затем кладёт его в образ вместе с backend.

```bash
docker build -f backend/Dockerfile -t peresadochnaya/api:local .
```

Запуск с локальным OTP и смонтированными данными:

```bash
docker run --rm -p 8000:8000 \
  -e PLANNER_MODE=otp \
  -e OTP_URL=http://host.docker.internal:8080 \
  -v "$PWD/data/gtfs:/data/gtfs:ro" \
  -v "$PWD/otp/data:/data/otp:ro" \
  --add-host=host.docker.internal:host-gateway \
  peresadochnaya/api:local
```

Frontend и API будут доступны на <http://localhost:8000> (`/`, `/api/v1`,
`/docs`). На Linux флаг `--add-host` нужен, чтобы контейнер видел OTP на хосте.

Отдельный образ realtime-сервиса:

```bash
docker build -f realtime/Dockerfile -t peresadochnaya/realtime:local .
```

## Тесты

Команды повторяют CI (`.github/workflows/backend.yml`).

```bash
# backend
cd backend && ../.venv/bin/python -m pytest tests

# данные и скрипты OTP
cd ..
.venv/bin/python -m pytest data/gtfs/tests data/metro/tests data/rail/tests
.venv/bin/python -m pytest otp/scripts/tests

# realtime: C++-движок + Python-тесты
.venv/bin/pip install -r realtime/requirements.txt
mkdir -p .bench-data
g++ -std=c++17 -O2 -Wall -Wextra -Werror \
  realtime/cpp/delay_engine.cpp -o .bench-data/delay-engine
DELAY_ENGINE_BINARY="$PWD/.bench-data/delay-engine" PYTHONPATH=realtime \
  .venv/bin/python -m pytest realtime/tests

# frontend
cd frontend
npm ci
npm test
npm run build
```

## Переменные окружения

Основные переменные backend:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `PLANNER_MODE` | `unavailable` | `dummy` — демо, `otp` — реальный OpenTripPlanner, `unavailable` — без планировщика |
| `OTP_URL` | `http://localhost:8080` | Адрес OTP |
| `OTP_TIMEOUT_SECONDS` | `20` | Таймаут запроса к OTP |
| `DATA_ROOT` | `/data` | Каталог с `gtfs/`, `metro/`, `rail/`, `otp/` |
| `FRONTEND_DIST` | `/app/frontend/dist` | Собранный frontend для раздачи с `/` |
| `CORS_ORIGINS` | пусто | Разрешённые origin через запятую |
| `YANDEX_GEOCODER_API_KEY`, `YANDEX_SUGGEST_API_KEY` | пусто | Геокодер и подсказки адресов |
| `YANDEX_ROUTER_API_KEY`, `YANDEX_JS_API_KEY` | пусто | Дорожные маршруты и ключ Яндекс.Карт |
| `DGIS_API_KEY` | пусто | 2ГИС |
| `REALTIME_URL` | пусто | Адрес realtime-сервиса для реальных позиций транспорта |

Frontend: `VITE_API_URL` задаёт базовый URL API (по умолчанию пусто —
используются относительные запросы `/api/v1`). Ключи Яндекс.Карт, геокодера и
маршрутизации задаются на backend и в k3s-секретах (`YANDEX_JS_API_KEY`,
`YANDEX_GEOCODER_API_KEY`, `YANDEX_SUGGEST_API_KEY`, `YANDEX_ROUTER_API_KEY`);
подробности — в `deploy/README.md`. Параметры realtime-сервиса — в
`realtime/README.md`.

## Данные и их обновление

- **Наземный GTFS** — `data/gtfs/versioned_static.py`, версионное хранилище
  `data/gtfs`. В k3s обновляется CronJob каждые 20 минут.
- **Метро** — `data/metro/refresh.py`, оценочный GTFS на основе официальных
  интервалов и времён pMetro. Календарь действует 30 дней, поэтому фид и граф
  нужно обновлять вместе.
- **Электрички** — `data/rail/refresh.py`, датированный GTFS из API
  Яндекс.Расписаний. Требует `YANDEX_RASP_API_KEY`; горизонт расписания
  ограничен загруженными датами.
- **GTFS-RT** — `data/gtfs/live_vehicles.py` и сервис `realtime/`; отдают
  позиции транспорта. Источник не содержит `trip_id`, поэтому надёжные
  TripUpdates/ETA сейчас не строятся.

После обновления фидов граф OTP нужно пересобрать:
`cd otp && ./scripts/build-graph.sh` (при необходимости `--force`), затем
пересоздать контейнер `docker compose up -d --force-recreate otp`.

## Развёртывание

Production работает в k3s. Push в `main` запускает CI: тесты, сборку
frontend, публикацию образов в GHCR и выкладку манифестов через SSH/NetBird.
Живой адрес — <https://peresadochnaya.lol>. Ручная выкладка и требования к
секретам описаны в `deploy/README.md`.

## Структура репозитория

```text
backend/        FastAPI API, адаптер OTP, тарифы, GTFS-индекс
frontend/       React + Vite + TypeScript + Tailwind, Яндекс.Карты
otp/            конфигурация OTP, сборка и версионирование графа
data/           загрузчики GTFS (наземный, метро, электрички), GTFS-RT
realtime/       GTFS-RT сервис и C++-движок расчёта задержек
deploy/         манифесты k3s, ingress, CI-выкладка
docs/           проектные заметки и спецификации
research/       исследовательские материалы и замеры
openapi.yaml    сгенерированный OpenAPI-контракт
```

## Документация

| Документ | О чём |
|---|---|
| `backend/README.md` | API, демо/реальные режимы, тарифы, переменные окружения |
| `frontend/README.md` | Структура frontend и компоненты |
| `otp/README.md` | Сборка графа, версии, конфигурация OTP, откат |
| `data/README.md` | GTFS, метро, электрички, GTFS-RT, контракты данных |
| `realtime/README.md` | Realtime-сервис, C++-движок, ограничения источника |
| `deploy/README.md` | k3s, PVC, секреты, CI/CD, ingress |

## Ограничения

- По умолчанию API работает в демо-режиме. Для реальных маршрутов нужны
  `dummy=false`, `PLANNER_MODE=otp`, запущенный OTP и ключи провайдеров.
- Метро описано оценочным GTFS: частоты, времена pMetro и 30-дневный календарь;
  это не официальное расписание.
- Расписание электричек ограничено датами загруженного снимка.
- GTFS-RT даёт координаты машин, но не привязку к рейсу; ETA по пробкам и
  realtime-поправки OTP пока не гарантируются.
- Тарифы рассчитывает backend по участкам (legs) из ответа OTP; OTP не хранит цены и не
  оптимизирует маршрут по точной стоимости.
