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
  dummyMode: boolean;
  loadStops: (bbox: string) => Promise<GtfsStop[]>;
}

const MODE_COLORS: Record<LegMode, string> = {
  walk: '#94A3B8',      // Slate / Серый
  bus: '#2563EB',       // Синий
  tram: '#DC2626',      // Красный
  trolleybus: '#16A34A', // Зеленый
  metro: '#CA8A04',     // Золотой / Желтый
  rail: '#9333EA',      // Фиолетовый
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
  const pointsRef = useRef({ pointA, pointB });

  // Карта инициализируется один раз, поэтому ее обработчики должны читать
  // актуальные точки из ref, а не из замыкания первого рендера.
  useEffect(() => {
    pointsRef.current = { pointA, pointB };
  }, [pointA, pointB]);

  // Инициализация карты
  useEffect(() => {
    ymaps.ready(() => {
      if (!mapContainerRef.current || mapInstance.current) return;

      const map = new ymaps.Map(mapContainerRef.current, {
        center: [59.9386, 30.3141], // Санкт-Петербург по умолчанию
        zoom: 12,
        controls: ['zoomControl', 'geolocationControl']
      });

      routesCollectionRef.current = new ymaps.GeoObjectCollection();
      stopsCollectionRef.current = new ymaps.GeoObjectCollection();
      map.geoObjects.add(routesCollectionRef.current);
      map.geoObjects.add(stopsCollectionRef.current);

      // Клик по карте для установки точек
      map.events.add('click', (e: any) => {
        const coords = e.get('coords'); // [lat, lon]
        const clickedPoint = { lat: coords[0], lon: coords[1] };
        const current = pointsRef.current;

        if (!current.pointA) {
          pointsRef.current = { ...current, pointA: clickedPoint };
          onPointChange('A', clickedPoint);
        } else if (!current.pointB) {
          pointsRef.current = { ...current, pointB: clickedPoint };
          onPointChange('B', clickedPoint);
        }
      });

      // Подгрузка остановок при изменении видимой зоны
      const handleBoundsChange = async () => {
        if (map.getZoom() < 14) {
          stopsCollectionRef.current.removeAll();
          return;
        }
        const bounds = map.getBounds(); // [[latMin, lonMin], [latMax, lonMax]]
        // BBOX в формате minLon,minLat,maxLon,maxLat
        const bbox = `${bounds[0][1]},${bounds[0][0]},${bounds[1][1]},${bounds[1][0]}`;
        const stops = await loadStops(bbox);

        stopsCollectionRef.current.removeAll();
        stops.forEach((stop) => {
          const stopPlacemark = new ymaps.Placemark(
            [stop.lat, stop.lon],
            { hintContent: stop.name },
            {
              preset: 'islands#circleDotIcon',
              iconColor: '#0284c7'
            }
          );
          stopPlacemark.events.add('click', () => onSelectStop(stop));
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

  // Синхронизация маркера точки А
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

  // Синхронизация маркера точки Б
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

  // Отрисовка маршрутов и их сегментов
  useEffect(() => {
    if (!routesCollectionRef.current || !mapInstance.current) return;
    routesCollectionRef.current.removeAll();

    if (!routes.length) return;

    routes.forEach((route, index) => {
      const isSelected = index === selectedRouteIndex;

      if (!isSelected) {
        // Фоновый маршрут рисуется единой серой линией
        // Важно: координаты в LineString хранятся как [lon, lat], в Yandex Maps передаем [lat, lon]
        const coords = route.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
        const polyline = new ymaps.Polyline(coords, {}, {
          strokeColor: '#94A3B8',
          strokeWidth: 4,
          strokeOpacity: 0.4
        });
        routesCollectionRef.current.add(polyline);
      } else {
        // Выбранный маршрут рендерится по участкам (Legs) с цветовым кодированием
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

    // Масштабирование карты под выбранный маршрут
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