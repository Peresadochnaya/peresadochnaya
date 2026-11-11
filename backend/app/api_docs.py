"""Shared API explanations for generated OpenAPI and the root YAML contract."""

API_DESCRIPTION = (
    'Контракт front/back. Справочные и геосервисные ручки реализованы. /routes возвращает '
    'dummy-ответ; реальный поиск ещё не подключён. MVP реального поиска — только OTP. '
    'mode=real означает расчёт планировщика и не обещает пробки или realtime: их фактические '
    'источники указаны в sources.'
)

OPERATION_DESCRIPTIONS = {
    '/health/ready': (
        'Готовность HTTP API. Доступность GTFS и графа планировщика проверяется отдельно '
        'через /api/v1/data/status.'
    ),
    '/api/v1/suggest': (
        'Address suggestions biased towards Saint Petersburg and Leningrad Oblast. Resolve '
        'the selected URI through the geocoder to obtain coordinates. The selected provider is contacted '
        'only with dummy=false.'
    ),
    '/api/v1/geocode': (
        'Resolve exactly one of q (address) or uri (selected suggestion). At most one result;'
        ' no match yields items=[].'
    ),
    '/api/v1/reverse': (
        'Resolve coordinates to an address. Result coordinates belong to the found object; '
        'keep the original clicked point for routing.'
    ),
    '/api/v1/road-route': (
        'Driving estimate for exactly two ordered points, independently of OTP. One traffic '
        'mode per call, at most one selected provider request with dummy=false. Not a public transport '
        'route. No automatic traffic comparison.'
    ),
    '/api/v1/routes': (
        'Контракт поиска поездки A → B. В MVP используется только OTP: providers=[otp], без '
        'Яндекс/2ГИС-поправок и без GTFS-RT. Реальный поиск пока не подключён: dummy=false '
        'возвращает 503 planner_unavailable. providers — массив источников кандидатов, '
        'отдельный от режима dummy. Позднее варианты OTP, Яндекса и 2ГИС объединяются и '
        'переранжируются; provenance в каждом варианте сохраняет происхождение кандидата и '
        'фактически применённые поправки. modes — массив видов транспорта; '
        'walk_speed=slow|normal|fast и less_walking — предпочтения для OTP. passenger задаёт '
        'один из пяти профилей и uncovered_payment для поездок вне покрытия проездного. '
        'Сейчас backend принимает эти поля только как контракт, возвращает явно синтетический'
        ' ответ либо 503 planner_unavailable и не применяет предпочтения к реальным '
        'маршрутам. Для метро доступны справочные интервалы, режимы работы, топология и '
        'переходы из data/processed/metro. Половина модельного интервала допускается как явно'
        ' помеченная эвристика ожидания с metro_wait_estimate, но не как точное расписание. '
        'Начало/конец работы проверяются по направлению и станции. Праздничные дни и '
        'изменённый режим требуют отдельного календаря. Отдельный оценочный GTFS метро '
        'собирается data.metro.refresh: времена перегонов и переходов берутся из pMetro и '
        'имеют duration_source=estimated. Это справочные оценки, а не официальное расписание.'
        ' Исходные CSV связей остаются без длительностей; обогащённые таблицы и происхождение'
        ' данных лежат в metro_reference.json внутри GTFS ZIP.'
    ),
    '/api/v1/vehicles': (
        'Текущие позиции транспорта из GTFS-RT VehiclePosition.\nКэш сервера 15–30 с. Если фид'
        ' молчит дольше 2× интервала — stale=true.\nЕсли лимит достигнут и есть ещё — warning '
        'vehicles_truncated.\n'
    ),
    '/api/v1/stops': (
        'Список остановок и станций в bbox. Фильтры q и modes применяются до пагинации; modes'
        ' означает наличие обслуживающей линии указанного типа. Порядок stop_id стабилен в '
        'пределах снимка. Нет совпадений — items=[], total=0. total — число совпадений до '
        'пагинации. Данные читаются из одного локального снимка, идентификаторы относятся к '
        'gtfs_version. Нет загруженного снимка — 503 gtfs_unavailable. Запрос не запускает '
        'обновление GTFS.'
    ),
    '/api/v1/stops/{stop_id}': (
        'Название, координаты и доступные сведения из stops.txt, а также обслуживающие '
        'транспортные линии. Поле description соответствует stop_desc; почтовый адрес не '
        'выдумывается. Неизвестный stop_id — 404 stop_not_found. Данные читаются из одного '
        'локального снимка, идентификаторы относятся к gtfs_version. Нет загруженного снимка '
        '— 503 gtfs_unavailable. Запрос не запускает обновление GTFS.'
    ),
    '/api/v1/stops/{stop_id}/departures': (
        'Отправления из локального снимка GTFS. Для точного расписания scheduled_departure_at'
        ' содержит ISO-время с offset. Для frequencies.txt exact_times=0 возвращается '
        'schedule_type=frequency, scheduled_departure_at=null, headway_seconds и окно '
        'frequency_start_at/frequency_end_at; это интервал, не обещание конкретной посадки. '
        'Частотное окно пересекается с запрошенным окном; сортировка по точному времени либо '
        'началу пересечения. Учитываются service day, исключения calendar_dates, времена '
        '>=24:00:00 и часовой пояс перевозчика. Некорректный календарь — 422 '
        'invalid_service_date. Доступ к внешним API не требуется.'
    ),
    '/api/v1/transit-routes': (
        'Номера, названия и виды транспорта из routes.txt. Это линии ОТ, а не результаты '
        'планирования поездки A → B. Фильтры применяются до пагинации; сортировка по '
        'route_id. Нет совпадений — items=[], total=0. Данные читаются из одного локального '
        'снимка, идентификаторы относятся к gtfs_version. Нет загруженного снимка — 503 '
        'gtfs_unavailable. Запрос не запускает обновление GTFS.'
    ),
    '/api/v1/transit-routes/{route_id}': (
        'Линия, направления и страница связанных рейсов из trips.txt, независимо от '
        'календарной активности на текущую дату. direction_id=0/1 не означает универсально '
        'прямое/обратное направление: для подписи используется headsign. Направление может '
        'отсутствовать. trips отсортированы по trip_id; offset, limit, total, has_more '
        'относятся только к trips. Неизвестная линия — 404 transit_route_not_found. Данные '
        'читаются из одного локального снимка, идентификаторы относятся к gtfs_version. Нет '
        'загруженного снимка — 503 gtfs_unavailable. Запрос не запускает обновление GTFS.'
    ),
    '/api/v1/trips/{trip_id}': (
        'Рейс с service_id, остановками по stop_sequence, временами stop_times и геометрией '
        'shapes.txt. Порядок времени — сервисный день GTFS: 25:10:00 означает 01:10 '
        'следующего календарного дня. Времена возвращаются исходными строками, не приводятся '
        'к 00–23 и не обещают активности рейса сегодня. Время промежуточной остановки может '
        'отсутствовать; в таком случае null, без выдуманной интерполяции. Геометрия — '
        'LineString в [lon,lat], либо null с предупреждением, если shape отсутствует. '
        'Неизвестный trip_id — 404 trip_not_found. Данные читаются из одного локального '
        'снимка, идентификаторы относятся к gtfs_version. Нет загруженного снимка — 503 '
        'gtfs_unavailable. Запрос не запускает обновление GTFS.'
    ),
}

ROUTE_WARNINGS_DESCRIPTION = (
    'Предупреждения. В dummy-ответе возвращается поясняющий текст. Для будущих адаптеров '
    'предусмотрены коды: code или code:k=v,k2=v2.\nИзвестные коды:\n  otp_timeout, '
    'otp_unavailable, otp_no_routes,\n  traffic_disabled, yandex_router_failed:leg=N, '
    'yandex_router_timeout:leg=N, yandex_quota_exceeded,\n  tight_transfer:leg=N, '
    'may_miss_transfer:leg=N,\n  gtfs_rt_stale, gtfs_rt_unavailable,\n  departure_in_past, '
    'departure_far_future_traffic_off,\n  walk_too_long, no_ground_transport\n'
)
