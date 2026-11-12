import React, { useEffect, useRef } from 'react';
import { Point, Route, GtfsStop, LegMode } from '../types/api';

declare const ymaps: any;

interface YandexMapProps {
  pointA: Point | null;
  pointB: Point | null;
  onPointChange: (type: 'A' | 'B', point: Point) => void;
  routes: Route[];
  selectedRouteIndex: number;
  onSelectStop: (stop: GtfsStop) => void;
  loadStops: (bbox: string) => Promise<GtfsStop[]>;
}

const MODE_COLORS: Record<LegMode, string> = {
  walk: '#94A3B8',
  bus: '#2563EB',
  tram: '#DC2626',
  trolleybus: '#16A34A',
  metro: '#CA8A04',
  rail: '#9333EA',
  wait: '#CBD5E1'
};

export const YandexMap: React.FC<YandexMapProps> = ({
  pointA,
  pointB,
  onPointChange,
  routes,
  selectedRouteIndex,
  onSelectStop,
  loadStops
}) => {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapInstance = useRef<any>(null);
  const markerARef = useRef<any>(null);
  const markerBRef = useRef<any>(null);
  const routesCollectionRef = useRef<any>(null);
  const stopsCollectionRef = useRef<any>(null);

  // Ссылки на актуальные точки для событий клика
  const pointAState = useRef(pointA);
  const pointBState = useRef(pointB);
  useEffect(() => { pointAState.current = pointA; }, [pointA]);
  useEffect(() => { pointBState.current = pointB; }, [pointB]);

  // Инициализация карты
  useEffect(() => {
    ymaps.ready(() => {
      if (!mapContainerRef.current || mapInstance.current) return;

      const map = new ymaps.Map(
        mapContainerRef.current,
        {
          center: [59.9386, 30.3141],
          zoom: 13,
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
          onPointChange('A', clickedPoint);
        } else if (!pointBState.current) {
          onPointChange('B', clickedPoint);
        }
      });

      // Загрузка остановок через бэкенд /api/v1/stops при изменении BBOX
      const handleBoundsChange = async () => {
        if (map.getZoom() < 14) {
          stopsCollectionRef.current.removeAll();
          return;
        }
        const bounds = map.getBounds();
        // Формат BBOX: minLon,minLat,maxLon,maxLat
        const bbox = `${bounds[0][1].toFixed(6)},${bounds[0][0].toFixed(6)},${bounds[1][1].toFixed(6)},${bounds[1][0].toFixed(6)}`;
        const stops = await loadStops(bbox);

        stopsCollectionRef.current.removeAll();
        stops.forEach((stop) => {
          // Создаем визуальную метку остановки
          const stopPlacemark = new ymaps.Placemark(
            [stop.lat, stop.lon],
            { hintContent: `Остановка: ${stop.name}` },
            {
              preset: 'islands#nightCircleIcon',
              iconColor: '#0284c7',
              hasBalloon: false // Не открываем балун Яндекса, работаем через наш интерфейс
            }
          );

          // Клик по остановке открывает наше окно с /api/v1/stops/{stop_id}/departures
          stopPlacemark.events.add('click', (e: any) => {
            e.stopPropagation();
            onSelectStop(stop);
          });

          stopsCollectionRef.current.add(stopPlacemark);
        });
      };

      map.events.add('boundschange', handleBoundsChange);
      mapInstance.current = map;
    });

    return () => {
      if (mapInstance.current) {
        mapInstance.current.destroy();
        mapInstance.current = null;
      }
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
          { preset: 'islands#darkGreenIcon', draggable: true }
        );
        marker.events.add('dragend', () => {
          const coords = marker.geometry.getCoordinates();
          onPointChange('A', { lat: coords[0], lon: coords[1] });
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
          { preset: 'islands#redIcon', draggable: true }
        );
        marker.events.add('dragend', () => {
          const coords = marker.geometry.getCoordinates();
          onPointChange('B', { lat: coords[0], lon: coords[1] });
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

      if (!isSelected) {
        const coords = route.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
        const polyline = new ymaps.Polyline(coords, {}, {
          strokeColor: '#94A3B8',
          strokeWidth: 4,
          strokeOpacity: 0.35
        });
        routesCollectionRef.current.add(polyline);
      } else {
        route.legs.forEach((leg) => {
          if (!leg.geometry?.coordinates?.length) return;
          const legCoords = leg.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
          const color = MODE_COLORS[leg.mode] || '#2563EB';

          const legPolyline = new ymaps.Polyline(legCoords, {
            hintContent: leg.line?.short_name ? `${leg.mode}: ${leg.line.short_name}` : leg.mode
          }, {
            strokeColor: color,
            strokeWidth: 6,
            strokeOpacity: 0.9,
            strokeStyle: leg.mode === 'walk' ? 'dash' : 'solid'
          });

          routesCollectionRef.current.add(legPolyline);
        });
      }
    });

    const activeRoute = routes[selectedRouteIndex];
    if (activeRoute?.geometry?.coordinates?.length) {
      const coords = activeRoute.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
      mapInstance.current.setBounds(ymaps.util.bounds.fromPoints(coords), {
        checkZoomRange: true,
        zoomMargin: 40
      });
    }
  }, [routes, selectedRouteIndex]);

  return <div ref={mapContainerRef} className="w-full h-full relative" />;
};
