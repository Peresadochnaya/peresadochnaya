# Модуль данных GTFS

Пути в таблице указаны от каталога `data`. Команды ниже запускайте из корня репозитория; его локальное имя может быть любым.

## Файлы

| Путь | Назначение |
| --- | --- |
| `raw/portal-gtfs.zip` | Текущий GTFS Schedule для загрузки в OTP |
| `raw/portal-gtfs.previous.zip` | Предыдущий ZIP для отката |
| `processed/gtfs/*.csv` | Таблицы текущего ZIP для backend; имена как в GTFS, расширение `.csv` |
| `processed/gtfs/manifest.json` | SHA-256 исходного ZIP и размеры подготовленных файлов |
| `gtfs/refresh_static.py` | Скачивает ZIP и обновляет CSV при изменении |
| `gtfs/live_vehicles.py` | Запрашивает GTFS-RT и выводит положения машин в JSON |
| `requirements.txt` | Зависимости двух Python-скриптов |
| `gtfs/certs/spb_gtfs_ca_bundle.pem` | Публичные CA-сертификаты для проверки HTTPS портала |
| `gtfs/tests/` | Тесты обновления ZIP, CSV и разбора положений машин |

## Подготовка после клонирования

Проверено на Python 3.14. Из корня репозитория создайте окружение и установите зависимости:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r data/requirements.txt
```

ZIP вручную скачивать не нужно: `.venv/bin/python data/gtfs/refresh_static.py` получит его с портала и построит CSV. Для `live_vehicles.py` нужен доступ к порталу и установленные зависимости; ZIP ему не требуется. Публичный CA-файл уже лежит в `data/gtfs/certs/`. Текущий `raw/portal-gtfs.zip` можно передать команде через Git; резервный ZIP и подготовленные CSV игнорируются.

## Функции

Классов в этом модуле нет. Основные функции:

| Функция | Что делает |
| --- | --- |
| `refresh_static.sha256` | Считает хэш файла для сравнения версий |
| `refresh_static.inspect_archive` | Проверяет ZIP, обязательные таблицы и календарь на дату |
| `refresh_static.prepare_csv` | Извлекает таблицы `.txt` как `.csv`, сохраняет хэш ZIP в manifest |
| `refresh_static.activate_archive` | Заменяет ZIP и сохраняет одну предыдущую версию |
| `refresh_static.main` | Скачивает ZIP и печатает JSON с результатом |
| `live_vehicles.build_snapshot` | Разбирает protobuf и формирует JSON с возрастом каждой позиции |
| `live_vehicles.fetch_snapshot` | Запрашивает свежий protobuf по HTTPS |
| `live_vehicles.show_positions` | Печатает человекочитаемый список машин |
| `live_vehicles.moscow_time` | Форматирует Unix-время для консоли |
| `live_vehicles.main` | Обрабатывает параметры CLI и выводит снимок или JSON Lines |

`spb_gtfs_ca_bundle.pem` не является токеном или секретом: он нужен `requests` для проверки сертификата сервера. Системные настройки сертификатов не меняются.

## Контракт для backend

В проекте два входа: GTFS Schedule ZIP для OTP и GTFS-RT Vehicle Positions для backend. Собственного HTTP API и запущенного OTP в `data` нет.

### 1. GTFS Schedule → OTP

```bash
.venv/bin/python data/gtfs/refresh_static.py
```

Скрипт получает `https://transport.orgp.spb.ru/Portal/transport/internalapi/gtfs/feed.zip`, проверяет TLS и ZIP, сохраняет его в `raw/portal-gtfs.zip`, а все таблицы `.txt` извлекает в `processed/gtfs/` с расширением `.csv`. Содержимое CSV не меняется. При изменении прежний ZIP остаётся в `raw/portal-gtfs.previous.zip`.

**Выход:** одна строка JSON в `stdout`; при ошибке — сообщение в `stderr` и код процесса `1`.

```json
{"status":"unchanged","sha256":"<hash>","previous_sha256":"<hash>","routes":547,"stops":8811,"invalid_calendar_rows":1013,"active_service_ids":0,"latest_exception_date":"20261104","service_date":"20261110","warning":"Нет действующих service_id на указанную дату","csv_status":"updated","csv_dir":"<data-path>/processed/gtfs","csv_files":14,"checked_at":"<ISO-8601>","archive":"<data-path>/raw/portal-gtfs.zip"}
```

| Поле | Действие команды |
| --- | --- |
| `status=updated` | Передать новый ZIP планировщику и пересобрать граф OTP |
| `status=unchanged` | Использовать текущий граф, если он уже построен из этого `sha256` |
| `csv_status=updated` | Перечитать подготовленные CSV; бывает и при первом запуске с неизменным ZIP |
| `csv_status=unchanged` | Подготовленные CSV уже соответствуют `sha256` |
| `active_service_ids=0` или `warning` | Не считать расписание пригодным для поездок на `service_date`; проверить источник |
| Код процесса `1` | Оставить прежний ZIP и граф; зарегистрировать ошибку |

OTP использует `routes.txt`, `stops.txt`, `trips.txt`, `stop_times.txt`, календарь и геометрию из ZIP. Для маршрута от координаты A до B дополнительно нужен OSM PBF. На **10 ноября 2026 года** в текущем архиве нет активных `service_id`; транспортных вариантов на эту дату OTP может не вернуть. Метро в архиве нет.

Backend может читать `processed/gtfs/routes.csv`, `stops.csv` и другие таблицы через `csv.DictReader(..., encoding="utf-8-sig")`. Перед использованием он сверяет `manifest.json.source_sha256` с `sha256` текущего ZIP. Не сканируйте многомиллионный `stop_times.csv` на каждый HTTP-запрос: построение маршрута остаётся задачей OTP.

### 2. GTFS-RT → backend

Один снимок всех машин:

```bash
.venv/bin/python data/gtfs/live_vehicles.py --all --json
```

Троллейбус №31:

```bash
.venv/bin/python data/gtfs/live_vehicles.py --route-id 1087 --json
```

Непрерывно: `.venv/bin/python data/gtfs/live_vehicles.py --all --json --watch 30`. Каждая строка `stdout` — **отдельный JSON-снимок**, без предыдущих снимков. `--all` снимает фильтр. Скрипт сам обращается к `https://transport.orgp.spb.ru/Portal/transport/internalapi/gtfs/realtime/vehicle`; предварительный `curl` не нужен.

**Контракт JSON:**

```json
{
  "source_url": "https://transport.orgp.spb.ru/Portal/transport/internalapi/gtfs/realtime/vehicle",
  "gtfs_realtime_version": "1.0",
  "fetched_at": 1794305262,
  "feed_timestamp": 1794305261,
  "feed_age_seconds": 1,
  "feed_is_stale": false,
  "route_filter": null,
  "vehicles": [{
    "vehicle_id": "26234", "label": "5101", "route_id": "1087",
    "trip_id": null, "stop_id": null,
    "latitude": 59.981716, "longitude": 30.328917,
    "bearing_deg": 49.0, "speed_mps": 3.333333,
    "observed_at": 1794305256, "age_seconds": 6, "is_stale": false
  }]
}
```

Это пример структуры, не текущие координаты. `feed_timestamp` — когда портал сформировал снимок; `observed_at` — когда измерены координаты **этой машины**; `fetched_at` — когда мы получили ответ. Все три поля — Unix seconds. `feed_age_seconds` и `age_seconds` показывают возраст в секундах на момент запроса. `feed_is_stale` и `is_stale` становятся `true`, если время отсутствует, старше 90 секунд или более чем на 30 секунд в будущем. Поэтому по `vehicle_id`, координатам и `observed_at` можно сказать, где и когда находилась машина. `/vehicle` отдаёт текущий снимок; для истории нужно сохранять последовательные ответы `--watch`.

Координаты — WGS84, скорость — м/с. `route_filter=null` при `--all`. Поля `trip_id`, `stop_id`, `bearing_deg`, `speed_mps`, `observed_at` могут быть `null`; `label` и `route_id` тоже могут отсутствовать в записи. `vehicles: []` означает, что по фильтру нет машин в этом снимке, а не ошибку запроса.

**Что делает backend с каждым снимком:**

1. Проверяет `feed_is_stale`; если `true`, не использует снимок как текущий.
2. Оставляет машины с `is_stale=false` и корректными координатами; проверяет аномальные скорости.
3. Сопоставляет `vehicle.route_id` со свежим `routes.txt` **той же версии ZIP** и отдаёт координаты фронту для карты.
4. При сетевой, TLS, HTTP или protobuf-ошибке не получает JSON, сохраняет последний снимок только с отметкой его возраста; следующий запрос делает через 30 секунд или позже.

В `/vehicle` нет надёжных `trip_id`, `stop_id`, готового ETA и поправки ко времени рейса. Позицию нельзя напрямую превратить в время прибытия или корректировку маршрута OTP.

### 3. Запрос маршрута A → B

Backend принимает от фронта `from: {lat, lon}`, `to: {lat, lon}` и время отправления с часовым поясом. Он запрашивает **запущенный командой OTP** через GraphQL, получает варианты `data.planConnection.edges[].node` и возвращает фронту их `legs` в порядке движения. Пример запроса к OTP 2 с координатами и датой, на которую в текущем ZIP есть активные `service_id`:

```bash
curl --request POST 'http://localhost:8080/otp/gtfs/v1' \
  --header 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{"query":"{ planConnection(origin: { location: { coordinate: { latitude: 59.9972, longitude: 30.2963 } } }, destination: { location: { coordinate: { latitude: 59.9431, longitude: 30.2789 } } }, dateTime: { earliestDeparture: \"2026-11-04T12:00:00+03:00\" }) { edges { node { start end legs { mode from { name } to { name } route { gtfsId shortName } } } } } }"}
JSON
```

`edges: []` означает, что вариантов не найдено; GraphQL `errors` или HTTP 4xx/5xx — ошибка запроса/сервера. Этот запрос **не проверен на проектном OTP**, потому что его конфигурации и сервера в репозитории нет. Endpoint и поля нужно сверить с `/graphiql` выбранной версией OTP. GTFS-RT позиции можно показывать рядом с маршрутом, но для переранжирования по пробкам backend нужна отдельная модель времени движения.

Адреса портала отвечали без токена при проверке 10 ноября 2026 года. [Официальные условия API](https://transport.orgp.spb.ru/help) отдельно предусматривают заявку и статический IP.
