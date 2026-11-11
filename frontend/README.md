src/
├── api/
│   └── client.ts         # Запросы к /api/v1/suggest, geocode, reverse, routes, stops, departures
├── types/
│   └── api.ts            # Строгая типизация по схеме openapi.json
├── components/
│   ├── YandexMap.tsx     # Интеграция с Яндекс Картами (точки A/B, полилинии, слой остановок)
│   ├── SearchPanel.tsx   # Инпуты A и B, автодополнение (suggest), фильтры поиска
│   ├── RouteResults.tsx  # Список маршрутов, переключение, детализация участков (Legs)
│   └── StopDrawer.tsx    # Просмотр информации об остановке и ближайших отправлений
├── App.tsx               # Главный контроллер состояния
└── main.tsx