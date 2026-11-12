"""Shared API explanations for generated OpenAPI and the root YAML contract."""

API_DESCRIPTION = (
    'Контракт front/back. dummy=true (по умолчанию) возвращает согласованное демо без '
    'внешних запросов; dummy=false включает реальные источники. Смысл флага сохранён. '
    'Ответ mode=dummy обозначает демонстрационные данные. '
    'mode=real означает расчёт планировщика; пробки и realtime описаны отдельно в sources. '
    'Реальный OTP подключается через PLANNER_MODE=otp. GTFS-RT и дорожные поправки пока не подключены. '
    'Необязательные debug-поля описывают будущую диагностику и сейчас равны null; '
    'после реализации полная диагностика возвращается по умолчанию без отдельного debug-флага. '
    'Согласованный TTL дорожных оценок — 1800 секунд (30 минут) в обычном и демо-сценарии; '
    'это не означает отсутствие пробок или подтверждение пересадок. '
    'Демо использует общий GTFS сети линии 193 '
    'с явно синтетическим расписанием раз в 10 минут.'
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
        'dummy=true возвращает согласованный демонстрационный маршрут по общей сети GTFS '
        'линии 193: пеший подход, ожидание по синтетическому расписанию раз в 10 минут, '
        'автобус и пеший участок до назначения. Если подходящего участка этой небольшой '
        'сети нет или автобус исключён, возвращается пеший пример. Это не реальный '
        'планировщик и не обещание оптимальности. В демо учитываются modes, exclude_modes '
        'и walk_speed; стоимость проездного не рассчитывается. Время начала округляется '
        'вверх до секунды. Идентификаторы линии, рейса и остановок совпадают со справочными '
        'ручками; времена автобуса совпадают с демо-отправлениями. '
        'dummy=false при PLANNER_MODE=otp вызывает OTP; без настройки возвращает '
        '503 planner_unavailable. Реальный MVP — только providers=[otp]; modes и '
        'exclude_modes, max_transfers, walk_speed и less_walking передаются в OTP. '
        'traffic_mode=on включает дорожную оценку в общем бюджете 15 секунд: до одного '
        'автомобильного A/B расчёта 2ГИС с альтернативами и до одного Яндекс-запроса '
        'с максимум 50 точками, без платных повторов. Кэш свеж 1800 секунд. '
        'Положительные поправки применяются к сопоставленным межостановочным частям; '
        'метро, железная дорога и подтверждённые выделенные пути не корректируются. '
        'После задержки интервального транспорта хвост перепланируется от остановки '
        'высадки; недостижимые невосстановленные посадки исключаются. '
        'Достаточное покрытие >=80% получает приоритет перед неполным; max_results общий. '
        'Если после восстановления хвоста варианты получили полностью одинаковые итоговые '
        'участки, возвращается один с диагностикой объединённых исходных вариантов; '
        'разные рейсы, времена и геометрии сохраняются. '
        'Полный debug всегда содержит диагностику, включая исключённые исходные варианты. '
        'GTFS-RT не подключён; фактические источники отмечены '
        'в provenance и sources. '
        'Оценочный GTFS метро отдельно собирается data.metro.refresh из официальной сети, '
        'интервалов и справочных времён pMetro; точное расписание поездов неизвестно. '
        'Ожидание метро может описываться metro_wait_estimate с duration_source=estimated. '
        'В метро-демо эти данные пока не используются.'
    ),
    '/api/v1/vehicles': (
        'dummy=true генерирует демонстрационные машины на общей сети GTFS линии 193 '
        'по тому же синтетическому 10-минутному расписанию, что и карточки отправлений. '
        'timestamp и updated_at — время генерации демо, не реальные GPS-наблюдения. '
        'Маркеры находятся на последних пройденных остановках своих рейсов. '
        'dummy=false возвращает реальные позиции из последнего успешного GTFS-RT снимка. '
        'bbox=minLon,minLat,maxLon,maxLat; фильтры bbox и modes применяются до limit '
        '(по умолчанию 10000, максимум 20000), превышение явно помечено vehicles_truncated. '
        'heading — направление по часовой стрелке от севера в градусах, null если источник '
        'его не передал. timestamp — время наблюдения, updated_at — метка исходного фида. '
        'Устаревшие данные помечены stale и gtfs_rt_stale. До первого снимка — 503 realtime_unavailable.'
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
