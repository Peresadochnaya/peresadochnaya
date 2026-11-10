# Backend API

Первый контракт для фронтенда. Реального OTP/Yandex adapter пока нет: `PLANNER_MODE=dummy` возвращает явно помеченный синтетический маршрут с пересадкой, `unavailable` (по умолчанию) возвращает 503. Dummy игнорирует дорожные условия и реальные расписания; его геометрия — прямые линии между вымышленными остановками.

Из корня репозитория:

```sh
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements-dev.txt
cd backend
PLANNER_MODE=dummy .venv/bin/uvicorn app.main:app --reload
.venv/bin/python -m pytest
```

OpenAPI: `/openapi.json`, интерактивный контракт: `/docs`.

| Endpoint | Ответ |
| --- | --- |
| `GET /health/live` | Процесс жив |
| `GET /health/ready` | HTTP API готов; не гарантия готовности planner/GTFS |
| `GET /api/v1/data/status` | Версии GTFS/графа, состояние и предупреждения |
| `GET /api/v1/ticket-profiles` | Доступные профили проездных с `coverage_status=not_implemented` |
| `POST /api/v1/routes` | Маршруты либо отдельная диагностируемая ошибка |

Запрос маршрута:

```json
{"points":[{"lat":60.002,"lon":30.296},{"lat":60.036,"lon":30.445}],"passenger":{"ticket_profile":"podorozhnik_wallet"},"departure_at":"2026-11-10T15:00:00+03:00","traffic_mode":"off","max_results":5}
```

`points` — упорядоченный массив точек маршрута: сейчас принимаются **строго две** (A и B). Ноль, одна, три и более точки, а также старый запрос с `from`/`to` дают HTTP 422. Промежуточные точки пока не поддерживаются. Поля `from`/`to` в ответе для концов отдельного сегмента сохраняются.

`passenger` необязателен, по умолчанию `{"ticket_profile":"full_fare"}`. Допустимые профили: `full_fare`, `podorozhnik_wallet`, `monthly_unified`, `monthly_surface`, `monthly_bus`, `monthly_metro`, `student_unified`, `student_surface`, `student_metro`, `school_unified`, `school_bus`, `concession_unified`. Человекочитаемые подписи доступны через `/api/v1/ticket-profiles`. Профиль обозначает заявленный билет, а не подтверждённое право на льготу. «Подорожник» — носитель; `podorozhnik_wallet` здесь означает единый электронный билет с денежным балансом. Проездные с разным покрытием представлены отдельными вариантами, а не одним признаком «студент».

Номенклатура сверена основным агентом с [официальными видами проездных](https://orgp.spb.ru/проездные/) и [зонами действия](https://orgp.spb.ru/зона-действия/). Эта версия API **не рассчитывает цены, применимость и покрытие билетов**, возвращает переданный профиль в `passenger` и явное предупреждение об этом. Ни нулевой цены, ни обещания бесплатного проезда dummy-ответ не выдаёт. Профили подготовлены как основа будущей тарификации и фильтров; автоматическое исключение транспортных сегментов сейчас не выполняется.

Время обязательно с часовым поясом; длительности в целых секундах; координаты WGS84; GeoJSON использует порядок `[lon,lat]`. Ответ включает `mode`, `routes[].legs`, `gtfs_version`, `graph_version`, `calculated_at`, `warnings`. Сегменты содержат исходную длительность, источник длительности и геометрию. Синтетические IDs имеют префикс `dummy:`.

Ошибка: `{"error":{"code":"planner_unavailable","message":"..."}}`, HTTP 503. Непригодная дата GTFS: `invalid_service_date`, HTTP 422. Ошибка входной схемы: стандартный FastAPI HTTP 422 с `detail`. В будущем корректный ответ планировщика без вариантов будет HTTP 200 с `mode=planner`, `routes=[]`; это отличается от недоступности сервиса. Пока интеграции planner нет, такой результат не симулируется.

`DATA_ROOT=/data`: API читает самодостаточный `gtfs/current.json` с `sha256` и метаданными текущей загрузки/проверки календаря. В ответе `manifest` содержит этот же снимок. Каталоги старых версий могут удаляться сразу после публикации, поэтому status endpoint не читает файлы по пути `versions/<sha256>` и не зависит от истории GTFS. Каждый ответ связывает SHA и календарные метаданные из одного атомарно прочитанного документа; следующий запрос может уже видеть новый документ. `gtfs/refresh-status.json` содержит последний результат обновления. Граф: `otp/current.json` с полем `version`. API не записывает эти файлы и не обновляет GTFS в HTTP-запросах. `active_service_ids=0` относится только к `service_date` текущей проверки, а не ко всем датам; окончательная проверка произвольной даты будет у planner adapter.

Контейнер собирается из корня репозитория: `docker build -f backend/Dockerfile .`; базовый Python поддерживает amd64/arm64. Переменная `PLANNER_MODE=dummy` нужна явно для демонстрационной выкладки.

Для фронтенда на другом origin укажите `CORS_ORIGINS=http://localhost:5173,https://frontend.example`; по умолчанию cross-origin доступ не включён. API не требует ключей для публичных read-only/demo endpoints.
