# OTP — модуль маршрутизации

**OpenTripPlanner (OTP) 2.11** — сервис multi-modal планирования маршрутов: общественный транспорт, пешие, велосипедные и автомобильные маршруты.

## Структура

```
```
otp/
├── config/                        # эталонная конфигурация OTP (в Git)
│   ├── otp-config.json            # фичи: DebugUi, GtfsGraphQlApi, ActuatorAPI
│   ├── build-config.json          # диапазон обслуживания, таймзона OSM
│   └── router-config.json         # routingDefaults (walk, numItineraries, searchWindow)
├── data/                          # данные и граф (не в Git)
│   ├── osm/spb-lo.osm.pbf         # OSM: СПб + Ленобласть, ~139 МБ
│   ├── incoming/portal-gtfs.zip   # GTFS для локальной отладки
│   ├── staging/                   # временный каталог сборки
│   ├── versions/<version>/        # опубликованный граф + три JSON-конфига
│   ├── current -> versions/<version>
│   ├── current.json               # метаданные активной версии
│   └── build-status.json          # результат последней сборки
├── scripts/
│   ├── build-graph.sh             # prepare + build + publish (основная команда)
│   ├── graph_store.py             # версионное хранилище графа
│   ├── entrypoint.sh              # запуск контейнера (build/serve)
│   ├── fetch-data.sh              # скачивание OSM-данных
│   ├── cut-osm.sh                 # разовая вырезка bbox из большого OSM
│   ├── fix-gtfs-calendar-dates.py # исправление битых дат в calendar.txt
│   └── tests/                     # тесты хранилища, без Docker и сети
├── Dockerfile
├── docker-compose.yml
└── .gitignore
```

Второй фид — оценочный GTFS метро — лежит вне `otp/`, в `data/metro`
(см. `data/README.md`).
```

## Как это устроено

OTP загружает `graph.obj` из базового каталога и не умеет выбирать версию. Поэтому
версия графа — это самодостаточный каталог `data/versions/<version>` с `graph.obj`
и тремя JSON-конфигами, а указатели `data/current` (симлинк) и `data/current.json`
публикуются атомарно.

В граф всегда попадают два GTFS-фида: наземный транспорт и оценочный GTFS метро.
OTP отличает фиды по имени файла, поэтому в `staging` они называются
`feed.gtfs.zip` и `metro.gtfs.zip`.

Версия вычисляется детерминированно:

```
version = sha256(pipeline_version, gtfs_fixed_sha256, metro_sha256, osm_sha256,
                 config_sha256, otp_version, otp_serialization_version_id)[:16]
```

`metro_sha256` входит в версию: обновление только метро пересобирает граф.
Если такая версия уже опубликована, сборка пропускается, а `prepare` заодно
активирует её, если указатель `current` от неё отстал (прерванный `publish`,
ручной откат). `config_sha256` не учитывает `configVersion`, который
подставляется уже после вычисления версии.

`data/current.json` содержит:

| Поле | Значение |
|---|---|
| `version` | идентификатор версии графа |
| `gtfs_source_sha256` | SHA-256 исходного GTFS из `data/gtfs` |
| `gtfs_fixed_sha256` | SHA-256 GTFS после исправления календаря |
| `gtfs_calendar_fixed` | применялся ли фикс календаря |
| `metro_sha256` | SHA-256 фида метро (пусто, если собран с `--no-metro`) |
| `osm_sha256`, `osm_file` | версия OSM, из которой собран граф |
| `otp_version`, `otp_serialization_version_id` | версия OTP, создавшая граф |
| `config_sha256` | хэш конфигурации без `configVersion` |
| `gtfs_service_start`, `gtfs_service_end` | диапазон дат в GTFS |
| `service_start`, `service_end` | фактический диапазон в графе (от OTP) |
| `graph_sha256`, `graph_bytes` | контроль целостности `graph.obj` |
| `prepared_at`, `built_at` | время подготовки и сборки |
| `activated_at`, `activated_by` | подтверждение загрузки графа сервером |

`build-status.json`: `status` (`prepared`, `updated`, `error`), `attempted_at`,
`last_success_at`, `version`, `error`. Он нужен, чтобы backend отличал
«граф ещё не собран» от «последняя сборка упала, работает предыдущая версия».

## Быстрый старт

### Предварительные требования

1. Docker с доступом к образу `opentripplanner/opentripplanner`.
2. Python 3.11+ для скриптов хранилища (только стандартная библиотека).
3. RAM: сборка графа шла успешно при `-Xmx6G`; сервер работает при `-Xmx4G`.
   На машине с 8 ГБ останавливайте контейнер `otp` перед сборкой.

### 1. OSM

`data/osm/spb-lo.osm.pbf` не хранится в Git. Файл получается один раз:

```bash
# скачать Северо-Западный ФО (~657 МБ) и вырезать СПб + ЛО (стратегия complete_ways)
./scripts/fetch-data.sh northwestern-fed-district-latest.osm.pbf
./scripts/cut-osm.sh data/incoming/northwestern-fed-district-latest.osm.pbf \
                    data/osm/spb-lo.osm.pbf
```

Готовый файл можно скопировать с другой машины. Зафиксированный SHA-256:

```
b48acafefa27b2895af7a76761ecf55d07702aa7a348bf24f242f15591b5796b  spb-lo.osm.pbf
```

`cut-osm.sh` — разовая операция, а не шаг регулярной сборки: OSM меняется редко,
а любое изменение OSM требует пересборки графа.

### 2. GTFS

Основной источник — версионное хранилище `data/gtfs` (см. `data/README.md`):

```bash
python -m data.gtfs.versioned_static --store-dir data/gtfs
```

Скрипт сборки читает `data/gtfs/current.json` и `versions/<sha>/feed.zip`.
Исходный ZIP не изменяется: исправление календаря применяется к копии в `staging`.

Для локальной отладки можно указать файл напрямую:

```bash
./scripts/build-graph.sh --gtfs-zip data/incoming/portal-gtfs.zip
```

> **Если `data/gtfs/current.json` отсутствует** (в репозитории этот каталог
> содержит только код, версия GTFS туда не коммитится), запуск без флагов
> завершится ошибкой `некорректный указатель GTFS`. В этом случае используйте
> `--gtfs-zip data/incoming/portal-gtfs.zip` либо сначала опубликуйте фид:
> `python -m data.gtfs.versioned_static --store-dir data/gtfs`.

### 2а. GTFS метро

Метро — второй, обязательный фид. Он собирается отдельно из официальной схемы
и времени перегонов pMetro:

```bash
python -m data.metro.refresh --store-dir data/metro
```

Результат — такое же версионное хранилище, что и `data/gtfs`:
`data/metro/current.json` и `versions/<sha256>/feed.zip`. Сборка графа берёт
`data/metro` по умолчанию и копирует фид в `staging` как `metro.gtfs.zip`.

> **Пропавший фид — ошибка, а не тихая сборка без подземки.** Если
> `data/metro/current.json` нет, `build-graph.sh` остановится с подсказкой.
> Осознанно собрать граф без метро можно только явно:
> `./scripts/build-graph.sh --no-metro` (или `METRO_STORE=""`).

Что важно про этот фид:

- расписание частотное (`frequencies.txt`, `exact_times=0`), интервалы — середина
  опубликованного диапазона, а не официальный график;
- календарь строится на 30 дней от даты сборки фида. Если граф не пересобирать
  месяц, метро исчезнет из выдачи — обновляйте `data/metro` и граф вместе;
- `shapes.txt` нет: геометрия поездки — прямые линии между станциями;
- пересадки заданы `transfers.txt` с минимальным временем перехода; станции
  разных линий — отдельные `stop_id` (`1:metro:kiv-tin`, `1:metro:mop-tin`).

### 3. Собрать и опубликовать граф

```bash
# если в data/gtfs опубликован фид
./scripts/build-graph.sh

# локальный режим: готовый ZIP из data/incoming
./scripts/build-graph.sh --gtfs-zip data/incoming/portal-gtfs.zip
```

Три шага внутри одного запуска:

1. `prepare` — копирует GTFS в `staging` как `feed.gtfs.zip`, добавляет
   `metro.gtfs.zip`, исправляет инвертированные `end_date` в копии, считает версию;
2. `build` — `docker compose run --rm otp-build` собирает `graph.obj`;
3. `publish` — переносит `staging` в `versions/<version>`, переключает
   `current` и `current.json`, оставляет текущую и предыдущую версии.

Если версия уже существует, сборка пропускается. Пересобрать принудительно:
`--force`. При ошибке сборки `build-status.json` получает `status=error`,
активная версия и работающий OTP не меняются.

> Имя GTFS-файла обязано содержать `gtfs`: OTP определяет тип файла по имени
> (шаблон `(?i)gtfs`), а не по содержимому. Файл `feed.zip` был бы молча
> проигнорирован, и граф собрался бы без транспорта.

### 4. Запустить сервер

```bash
docker compose up -d otp
```

OTP API доступен на `http://localhost:8080/`.

После публикации новой версии контейнер нужно пересоздать:
`docker compose up -d --force-recreate otp`. Сервис монтирует каталог `data`
целиком, а активную версию выбирает `OTP_HOMEDIR=/otp-data/current`. Симлинк
разрешается внутри контейнера, потому что Docker Desktop запоминает цель
симлинка при создании контейнера и после переключения версии продолжал бы
отдавать старый `graph.obj`.

```bash
docker compose up -d --force-recreate otp
python3 scripts/graph_store.py verify --url http://localhost:8080
```

`verify` сверяет `buildConfigVersion`/`routerConfigVersion` из `/otp/` с версией
в `current.json`, запрашивает `serviceTimeRange` и только после этого пишет
`activated_at`. Так `activated_at` означает реально загруженный граф, а не
просто опубликованный каталог.

`build-graph.sh --restart` делает пересоздание и проверку автоматически.
Если версия уже собрана, флаг тоже работает: `prepare` активирует её, а
`--restart` пересоздаёт контейнер и проверяет загрузку.

### Полезные команды

```bash
python3 scripts/graph_store.py status      # активная версия и статус сборки
docker compose logs -f otp                 # логи сервера
curl -s localhost:8080/otp/actuators/health
curl -s localhost:8080/otp/ | python3 -m json.tool
```

## API

**OTP 2.11 использует GraphQL.** Старый REST `/otp/routers/{id}/plan` больше не доступен.

| Эндпоинт | Описание |
|---|---|
| `POST /otp/gtfs/v1` | GraphQL API (маршрутизация, расписания, остановки) |
| `POST /otp/transmodel/v3` | GraphQL API (по стандарту Transmodel) |
| `GET /otp/` | Версия, версии конфигов, `otpSerializationVersionId` |
| `GET /otp/actuators/health` | Статус (`{"status": "UP"}`) |
| `GET /graphiql` | Редактор запросов |

### Пример: построить маршрут

```bash
curl -X POST 'http://localhost:8080/otp/gtfs/v1' \
  -H 'Content-Type: application/json' \
  -d '{
  "query": "query($from: PlanLabeledLocationInput!, $to: PlanLabeledLocationInput!, $dateTime: PlanDateTimeInput!, $first: Int) { planConnection(origin: $from, destination: $to, dateTime: $dateTime, first: $first) { routingErrors { code description } edges { node { start end duration numberOfTransfers legs { mode from { name } to { name } route { shortName longName } } } } } }",
  "variables": {
    "from": { "location": { "coordinate": { "latitude": 59.9343, "longitude": 30.3351 } } },
    "to": { "location": { "coordinate": { "latitude": 59.9447, "longitude": 30.2583 } } },
    "dateTime": { "earliestDeparture": "2026-11-15T09:00:00+03:00" },
    "first": 3
  }
}'
```

### Что важно учитывать при интеграции

- Метро приходит теми же легами `mode: SUBWAY`, `route.gtfsId` вида
  `1:metro:2` (префикс `1:` — номер загруженного фида). Проверка живости:
  `{ routes(transportModes: [SUBWAY]) { shortName gtfsId } }` должна вернуть
  6 линий.
- `routingErrors[].code = OUTSIDE_SERVICE_PERIOD` означает дату вне расписания.
  OTP при этом может вернуть пешеходный маршрут — это не маршрут на транспорте.
  Для метро это типичный симптом истёкшего 30-дневного календаря фида.
- `estimated` в `LegTime` заполняется только при наличии GTFS-RT. Сейчас
  `updaters: []`, поэтому используется `scheduledTime`.
- `legGeometry.points` — encoded polyline с точностью 5, порядок координат
  `(lat, lon)`.
- Дифференциация тарифов задаётся на каждый вид транспорта в
  `modes.transit.transit[].cost.reluctance` — это **множитель времени**, а не
  цена. Аддитивной стоимости поездки в OTP 2.11 нет, поэтому backend
  пересчитывает рубли в секунды через `value_of_time_rub_per_hour`
  (подробности — `backend/README.md`, раздел «Тарифы»). Проверено:
  `BUS reluctance=20` полностью убирает автобус из выдачи.
- `serviceTimeRange` возвращает диапазон в UNIX-секундах и обрезан настройками
  `transitServiceStart`/`transitServiceEnd` из `build-config.json`.

Backend использует этот API через `backend/app/otp.py`.

## Настройка

### Конфигурация

`config/build-config.json`:

```json
{
  "configVersion": "local",
  "transitServiceStart": "-P1Y",
  "transitServiceEnd": "P3Y",
  "transitModelTimeZone": "Europe/Moscow",
  "osmDefaults": { "timeZone": "Europe/Moscow" }
}
```

`config/router-config.json`:

```json
{
  "configVersion": "local",
  "updaters": [],
  "routingDefaults": {
    "walk": { "speed": 1.3, "reluctance": 4.0 },
    "numItineraries": 5,
    "searchWindow": "PT2H",
    "transferSlack": "PT2M",
    "accessEgress": { "maxDuration": "PT30M" }
  }
}
```

`configVersion` подменяется версией графа при сборке. Это позволяет проверить,
какую именно версию загрузил сервер.

### Почему `--abortOnUnknownConfig`

Entrypoint запускает OTP с `--abortOnUnknownConfig`. Раньше конфиг содержал
`routingDefaults.walkSpeed` и `routingDefaults.maxWalkDistance` — в OTP 2.11 таких
полей нет, и они молча игнорировались с предупреждением в логе. Теперь опечатка
в конфиге останавливает запуск.

Актуальные имена: `routingDefaults.walk.speed` и
`routingDefaults.accessEgress.maxDuration`.

### Память JVM

```yaml
environment:
  OTP_HEAP_SIZE: "4G"
  JAVA_OPTS: "-Xmx4G"
```

Для сборки — 6G (`otp-build` в `docker-compose.yml`).

### Реальное время (GTFS-RT)

`updaters` пуст. В `data` есть только Vehicle Positions, а `stop-time-updater`
OTP требует GTFS-RT TripUpdates. Позиции машин сами по себе не дают поправку
расписания, поэтому real-time в OTP не включён.

### Режимы работы

| Режим | Описание |
|---|---|
| `build` | Собрать граф в `OTP_HOMEDIR` и завершиться |
| `serve` | Загрузить `graph.obj` и запустить сервер |

Устанавливается через `OTP_MODE` или первый аргумент контейнера. `OTP_HOMEDIR`
задаёт базовый каталог OTP: у `otp-build` это `/var/opentripplanner` (staging),
у `otp` — `/otp-data/current`. В режиме `serve` каталог смонтирован только для
чтения, автоматической сборки нет: сначала `build-graph.sh`.

## Сброс и откат

Пересобрать текущую версию:

```bash
./scripts/build-graph.sh --force
```

Хранятся две версии: активная и предыдущая. Откат:

```bash
python3 scripts/graph_store.py status          # покажет список версий
cp data/versions/<previous>/manifest.json data/current.json
ln -sfn versions/<previous> data/current.tmp && mv -T data/current.tmp data/current
docker compose up -d --force-recreate otp
python3 scripts/graph_store.py verify --url http://localhost:8080
```

Откат не «залипает»: следующий `build-graph.sh` посчитает версию по текущим
входам и снова активирует её, если она уже собрана. Чтобы откатиться надолго,
нужно вернуть прежние входные данные (GTFS, метро, OSM или конфиги).

## Известные проблемы и фиксы

### 1. Битые даты в GTFS calendar.txt (ОРГП СПб)

Фид ОРГП содержит `end_date=20191230` во всех записях `calendar.txt` — заглушку
незаполненного поля. OTP 2.11 падает:

```
IllegalArgumentException: Invalid range, the end 2019-12-30
                        is before the start 2026-08-01
```

`graph_store.py prepare` применяет `fix-gtfs-calendar-dates.py` к копии фида в
`staging`. Исходный ZIP и контракт `data/gtfs` не меняются; в метаданных
сохраняются оба хэша. Скрипт меняет `end_date` только там, где
`end_date < start_date`, и работает побайтово в `latin-1`, не трогая
нестандартную кодировку остальных файлов.

### 2. OSM: Санкт-Петербург внутри Северо-Западного округа

Geofabrik не раздаёт Питер отдельным файлом. `cut-osm.sh` вырезает bbox
`28.17,59.32,33.59,61.09` со стратегией `complete_ways`: 139 МБ вместо 657 МБ,
дороги на границе не обрываются.

### 3. Zone.Identifier файлы

Появляются при скачивании из браузера Windows. Материализуются как обычные
файлы с именем `файл:Zone.Identifier`. Добавлены в `.gitignore`.

### 4. Healthcheck

В официальном образе нет `curl`/`wget`, а прежняя проверка обращалась к
удалённому REST-пути — контейнер считался `unhealthy` при работающем сервисе.
Healthcheck из `docker-compose.yml` убран; проверяйте с хоста:

```bash
curl -f http://localhost:8080/otp/actuators/health
```

## Отложенные решения

- **Автоматический перезапуск после публикации.** Сейчас это явный
  `docker compose up -d --force-recreate otp` или флаг `--restart`.
- **`OUTSIDE_SERVICE_PERIOD` → HTTP 422 `invalid_service_date`.** Решение
  отложено: сейчас backend возвращает walk-only вариант с предупреждением.
- **Kubernetes и CI/CD.** Модуль разворачивается локально. Тесты
  `scripts/tests` гоняются в общем workflow, но манифестов и сборки образа OTP
  в CI нет.

## Пин образа и совместимость графа

Базовый образ OTP запинен в `Dockerfile` по digest:

```
FROM opentripplanner/opentripplanner@sha256:11289dd093bae3845c2d2355a1845069de83e2809c14a0949dceb20a62359763
```

Это сборка `2.11.0-SNAPSHOT`, `otpSerializationVersionId=289`,
`commit f64e860dae6a`, `branch dev-2.x`, проверена 2026-11-11.
`docker compose build --pull` теперь безопасен — он всегда даст ту же сборку.

**Почему это важно.** `graph.obj` — Java-сериализация, совместимая только
с тем `otpSerializationVersionId`, который его создал. Тег `latest`
OTP перезаписывается при каждом dev-релизе, поэтому без пина одна и та же
команда сборки в разные дни даёт разные версии, и граф может перестать
загружаться.

**Что делать при смене digest.** Обновить строку `FROM`, пересобрать образ
и граф: `./scripts/build-graph.sh --force`, затем `verify`.

**Проверка при запуске.** `entrypoint.sh` в режиме `serve` читает
`otp_serialization_version_id` из `manifest.json` графа и сравнивает с
фактическим `ser.ver.id` образа. При расхождении контейнер падает с
понятным сообщением вместо ошибки загрузки графа. Флаг
`OTP_SKIP_VERSION_CHECK=1` отключает проверку.

## Файлы

| Файл | Назначение |
|---|---|
| `scripts/build-graph.sh` | Основная команда: prepare + build + publish (+ `--restart`, `--force`, `--no-metro`) |
| `scripts/graph_store.py` | Версии графа: `prepare`, `publish`, `verify`, `status`, `mark` |
| `scripts/entrypoint.sh` | Запуск OTP с базовым каталогом из `OTP_HOMEDIR`. Флаг `--port` — **только через пробел** |
| `scripts/fix-gtfs-calendar-dates.py` | Фикс инвертированных `end_date` в `calendar.txt` |
| `scripts/cut-osm.sh` | Разовая вырезка bbox через `osmium-tool` |
| `config/` | Эталонные конфиги; `configVersion` подставляет версию графа |

Метро-фид и его обновление описаны в `data/README.md` (`data/metro`).
