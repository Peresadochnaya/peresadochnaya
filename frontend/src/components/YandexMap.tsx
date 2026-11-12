import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Point, Route, GtfsStop, Leg, LegMode } from '../types/api';
import { getRouteOffsetMeters, offsetPolyline } from '../utils/routeGeometry';

declare const ymaps: any;

interface YandexMapProps {
  pointA: Point | null;
  pointB: Point | null;
  onPointChange: (type: 'A' | 'B', point: Point) => void;
  routes: Route[];
  selectedRouteIndex: number;
  onSelectStop: (stop: GtfsStop) => void;
  loadStops: (bbox: string) => Promise<GtfsStop[]>;
  dummyStops: boolean;
}

const MODE_COLORS: Record<LegMode, string> = {
  walk: '#7C3AED',
  bus: '#16A34A',
  tram: '#DC2626',
  trolleybus: '#2563EB',
  metro: '#CA8A04',
  rail: '#374151',
  wait: '#CBD5E1'
};

// Официальные цвета линий метро Санкт-Петербурга (как в data/metro/feed.py).
const METRO_LINE_COLORS: Record<number, string> = {
  1: '#D6083B',
  2: '#0078C9',
  3: '#009A49',
  4: '#EA7125',
  5: '#702785',
  6: '#A86B32'
};

const getMetroLineNumber = (leg: Leg): number | null => {
  if (leg.mode !== 'metro' || !leg.line) return null;

  for (const value of [leg.line.route_id, leg.line.short_name]) {
    const match = value.match(/(\d+)/);
    if (!match) continue;

    const line = Number(match[1]);
    if (line in METRO_LINE_COLORS) return line;
  }

  return null;
};

const getLegColor = (leg: Leg): string => {
  const metroLine = getMetroLineNumber(leg);
  return metroLine ? METRO_LINE_COLORS[metroLine] : MODE_COLORS[leg.mode] || '#2563EB';
};

export const YandexMap: React.FC<YandexMapProps> = ({
  pointA,
  pointB,
  onPointChange,
  routes,
  selectedRouteIndex,
  onSelectStop,
  loadStops,
  dummyStops
}) => {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapInstance = useRef<any>(null);
  const markerARef = useRef<any>(null);
  const markerBRef = useRef<any>(null);
  const routesCollectionRef = useRef<any>(null);
  const stopsCollectionRef = useRef<any>(null);
  const [mapZoom, setMapZoom] = useState(13);

  // Ссылки на актуальные точки для событий клика
  const pointAState = useRef(pointA);
  const pointBState = useRef(pointB);
  useEffect(() => { pointAState.current = pointA; }, [pointA]);
  useEffect(() => { pointBState.current = pointB; }, [pointB]);

  // Держим актуальные колбэки, не пересоздавая карту при каждом рендере
  const onPointChangeRef = useRef(onPointChange);
  useEffect(() => { onPointChangeRef.current = onPointChange; }, [onPointChange]);
  const onSelectStopRef = useRef(onSelectStop);
  useEffect(() => { onSelectStopRef.current = onSelectStop; }, [onSelectStop]);

  // Держим актуальный загрузчик остановок, не пересоздавая функцию отрисовки
  const loadStopsRef = useRef(loadStops);
  useEffect(() => { loadStopsRef.current = loadStops; }, [loadStops]);

  // Запрос и отрисовка остановок для текущего BBOX
  const fetchAndRenderStops = useCallback(async () => {
    if (!mapInstance.current || !stopsCollectionRef.current) return;

    const currentZoom = mapInstance.current.getZoom();
    // Показываем остановки начиная с 16 зума
    if (currentZoom < 16) {
      stopsCollectionRef.current.removeAll();
      return;
    }

    const bounds = mapInstance.current.getBounds();
    if (!bounds) return;

    // BBOX в формате minLon,minLat,maxLon,maxLat
    const bbox = `${bounds[0][1].toFixed(6)},${bounds[0][0].toFixed(6)},${bounds[1][1].toFixed(6)},${bounds[1][0].toFixed(6)}`;
    const stops = await loadStopsRef.current(bbox);

    // Карта могла быть уничтожена, пока шёл запрос
    if (!stopsCollectionRef.current) return;

    stopsCollectionRef.current.removeAll();

    stops.forEach((stop) => {
      // Создаем интерактивную метку с явным курсором 'pointer'
      const stopPlacemark = new ymaps.Placemark(
        [stop.lat, stop.lon],
        {
          hintContent: `<strong>${stop.name}</strong><br/><span style="color:#666">Нажмите для расписания</span>`
        },
        {
          preset: 'islands#blueDotIcon',
          iconColor: '#0284c7',
          cursor: 'pointer',
          hasBalloon: false,
          zIndex: 500 // Чтобы остановки были поверх линий маршрута
        }
      );

      // Клик по остановке открывает наше окно с /api/v1/stops/{stop_id}/departures
      stopPlacemark.events.add('click', (e: any) => {
        e.stopPropagation();
        onSelectStopRef.current(stop);
      });

      stopsCollectionRef.current.add(stopPlacemark);
    });
  }, []);

  // Перезагрузка остановок, если изменился режим dummy для остановок
  const previousDummyStops = useRef(dummyStops);
  useEffect(() => {
    if (previousDummyStops.current === dummyStops) return;

    previousDummyStops.current = dummyStops;
    if (!mapInstance.current) return;

    fetchAndRenderStops();
  }, [dummyStops, fetchAndRenderStops]);

  // Инициализация карты
  useEffect(() => {
    ymaps.ready(() => {
      if (!mapContainerRef.current || mapInstance.current) return;

      const map = new ymaps.Map(
        mapContainerRef.current,
        {
          center: [59.93428, 30.335098],
          zoom: 15, // Стартовый масштаб карты
          controls: [] // Убираем контролы по умолчанию с левой стороны
        },
        {
          suppressMapOpenBlock: true,
          // ВАЖНО: отключает встроенные всплывающие запросы Яндекса по объектам подложки
          yandexMapDisablePoiInteractivity: true
        }
      );

      // Добавляем зум и геолокацию СПРАВА, чтобы виджет слева их не перекрывал
      map.controls.add(new ymaps.control.ZoomControl({
        options: { position: { right: 16, top: 100 } }
      }));
      map.controls.add(new ymaps.control.GeolocationControl({
        options: { position: { right: 16, top: 220 } }
      }));

      routesCollectionRef.current = new ymaps.GeoObjectCollection();
      stopsCollectionRef.current = new ymaps.GeoObjectCollection();
      map.geoObjects.add(routesCollectionRef.current);
      map.geoObjects.add(stopsCollectionRef.current);

      // Клик по карте для последовательной установки точек
      map.events.add('click', (e: any) => {
        const coords = e.get('coords');
        const clickedPoint = { lat: coords[0], lon: coords[1] };

        if (!pointAState.current) {
          onPointChangeRef.current('A', clickedPoint);
        } else if (!pointBState.current) {
          onPointChangeRef.current('B', clickedPoint);
        }
      });

      // Перезапрос остановок при сдвиге или зуме карты
      map.events.add('boundschange', () => {
        const nextZoom = Math.round(map.getZoom());
        setMapZoom(prev => (prev === nextZoom ? prev : nextZoom));
        fetchAndRenderStops();
      });

      mapInstance.current = map;
      setMapZoom(Math.round(map.getZoom()));

      // Загружаем остановки сразу, не дожидаясь первого сдвига карты
      fetchAndRenderStops();
    });

    return () => {
      if (mapInstance.current) {
        mapInstance.current.destroy();
        mapInstance.current = null;
      }
      routesCollectionRef.current = null;
      stopsCollectionRef.current = null;
      markerARef.current = null;
      markerBRef.current = null;
    };
  }, []);

  // Синхронизация маркера точки А (включая корректное удаление)
  useEffect(() => {
    if (!mapInstance.current) return;
    if (pointA) {
      if (!markerARef.current) {
        const marker = new ymaps.Placemark(
          [pointA.lat, pointA.lon],
          { iconContent: 'A' },
          { preset: 'islands#darkGreenIcon', draggable: true, zIndex: 1000 }
        );
        marker.events.add('dragend', () => {
          const coords = marker.geometry.getCoordinates();
          onPointChangeRef.current('A', { lat: coords[0], lon: coords[1] });
        });
        mapInstance.current.geoObjects.add(marker);
        markerARef.current = marker;
      } else {
        markerARef.current.geometry.setCoordinates([pointA.lat, pointA.lon]);
      }
    } else if (markerARef.current) {
      mapInstance.current.geoObjects.remove(markerARef.current);
      markerARef.current = null;
    }
  }, [pointA]);

  // Синхронизация маркера точки Б (включая корректное удаление)
  useEffect(() => {
    if (!mapInstance.current) return;
    if (pointB) {
      if (!markerBRef.current) {
        const marker = new ymaps.Placemark(
          [pointB.lat, pointB.lon],
          { iconContent: 'Б' },
          { preset: 'islands#redIcon', draggable: true, zIndex: 1000 }
        );
        marker.events.add('dragend', () => {
          const coords = marker.geometry.getCoordinates();
          onPointChangeRef.current('B', { lat: coords[0], lon: coords[1] });
        });
        mapInstance.current.geoObjects.add(marker);
        markerBRef.current = marker;
      } else {
        markerBRef.current.geometry.setCoordinates([pointB.lat, pointB.lon]);
      }
    } else if (markerBRef.current) {
      mapInstance.current.geoObjects.remove(markerBRef.current);
      markerBRef.current = null;
    }
  }, [pointB]);

  // Отрисовка маршрутов
  useEffect(() => {
    if (!routesCollectionRef.current || !mapInstance.current) return;
    routesCollectionRef.current.removeAll();

    if (!routes.length) return;

    routes.forEach((route, index) => {
      const isSelected = index === selectedRouteIndex;
      const latitude = route.geometry.coordinates[0]?.[1] ?? 60;
      const offsetMeters = getRouteOffsetMeters(index, routes.length, mapZoom, latitude);

      if (!isSelected) {
        const coords = offsetPolyline(route.geometry.coordinates, offsetMeters)
          .map(([lon, lat]) => [lat, lon]);
        const polyline = new ymaps.Polyline(coords, {}, {
          strokeColor: '#94A3B8',
          strokeWidth: 4,
          strokeOpacity: 1
        });
        routesCollectionRef.current.add(polyline);
      } else {
        route.legs.forEach((leg) => {
          if (!leg.geometry?.coordinates?.length) return;
          const legCoords = offsetPolyline(leg.geometry.coordinates, offsetMeters)
            .map(([lon, lat]) => [lat, lon]);
          const color = getLegColor(leg);

          const legPolyline = new ymaps.Polyline(legCoords, {
            hintContent: leg.line?.short_name ? `${leg.mode}: ${leg.line.short_name}` : leg.mode
          }, {
            strokeColor: color,
            strokeWidth: 6,
            strokeOpacity: 1,
            strokeStyle: leg.mode === 'walk' ? 'dash' : 'solid'
          });

          routesCollectionRef.current.add(legPolyline);
        });
      }
    });
  }, [routes, selectedRouteIndex, mapZoom]);

  // Подгоняем карту под выбранный маршрут только при смене маршрута,
  // чтобы ручной зум пользователя не сбрасывался.
  useEffect(() => {
    const activeRoute = routes[selectedRouteIndex];
    if (!mapInstance.current || !activeRoute?.geometry?.coordinates?.length) return;

    const coords = activeRoute.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
    mapInstance.current.setBounds(ymaps.util.bounds.fromPoints(coords), {
      checkZoomRange: true,
      zoomMargin: 40
    });
  }, [routes, selectedRouteIndex]);

  return <div ref={mapContainerRef} className="w-full h-full relative" />;
};
