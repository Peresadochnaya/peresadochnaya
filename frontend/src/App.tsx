import React, { useState } from 'react';
import { YandexMap } from './components/YandexMap';
import { SearchPanel } from './components/SearchPanel';
import { RouteResults } from './components/RouteResults';
import { StopDrawer } from './components/StopDrawer';
import { Point, Route, GtfsStop, TransportMode, TicketProfile, DummyConfig } from './types/api';
import { api } from './api/client';

export const App: React.FC = () => {
  const [pointA, setPointA] = useState<Point | null>(null);
  const [pointB, setPointB] = useState<Point | null>(null);
  const [labelA, setLabelA] = useState('');
  const [labelB, setLabelB] = useState('');

  const [routes, setRoutes] = useState<Route[]>([]);
  const [selectedRouteIndex, setSelectedRouteIndex] = useState(0);
  const [isLoadingRoutes, setIsLoadingRoutes] = useState(false);

  const [selectedStop, setSelectedStop] = useState<GtfsStop | null>(null);

  // Конфигурация dummy по умолчанию: все true, как в спецификации
  const [dummyConfig, setDummyConfig] = useState<DummyConfig>({
    suggest: true,
    geocode: true,
    routes: true,
    stops: true,
    departures: true
  });

  // Установка точки при клике/драге на карте
  const handlePointChange = async (type: 'A' | 'B', point: Point) => {
    if (type === 'A') setPointA(point);
    else setPointB(point);

    try {
      const res = await api.reverseGeocode(point.lat, point.lon, dummyConfig.geocode);
      const text = res.items?.[0]?.label || `${point.lat.toFixed(4)}, ${point.lon.toFixed(4)}`;
      if (type === 'A') setLabelA(text);
      else setLabelB(text);
    } catch {
      const fallback = `${point.lat.toFixed(4)}, ${point.lon.toFixed(4)}`;
      if (type === 'A') setLabelA(fallback);
      else setLabelB(fallback);
    }
  };

  // Очистка точки
  const handleClearPoint = (type: 'A' | 'B') => {
    if (type === 'A') {
      setPointA(null);
      setLabelA('');
    } else {
      setPointB(null);
      setLabelB('');
    }
    setRoutes([]);
  };

  const handleSelectPlace = (type: 'A' | 'B', point: Point, label: string) => {
    if (type === 'A') {
      setPointA(point);
      setLabelA(label);
    } else {
      setPointB(point);
      setLabelB(label);
    }
  };

  const handleSwapPoints = () => {
    const tempP = pointA;
    const tempL = labelA;
    setPointA(pointB);
    setLabelA(labelB);
    setPointB(tempP);
    setLabelB(tempL);
  };

  // Поиск маршрутов
  const handleSearch = async (filters: {
    modes: TransportMode[];
    lessWalking: boolean;
    ticketProfile: TicketProfile;
  }) => {
    if (!pointA || !pointB) return;

    setIsLoadingRoutes(true);
    try {
      const res = await api.planRoute({
        points: [pointA, pointB],
        departure_at: new Date().toISOString(),
        traffic_mode: 'off',
        modes: filters.modes.length ? filters.modes : null,
        less_walking: filters.lessWalking,
        passenger: {
          ticket_profile: filters.ticketProfile,
          uncovered_payment: 'bank_card'
        },
        providers: ['otp'],
        max_results: 5
      }, dummyConfig.routes);

      setRoutes(res.routes || []);
      setSelectedRouteIndex(0);
    } catch (err) {
      console.error('Route planning error', err);
      alert('Ошибка при расчёте маршрута. Если реальный OTP ещё не подключён на бэкенде, включите флаг Dummy для /routes.');
    } finally {
      setIsLoadingRoutes(false);
    }
  };

  // Загрузка остановок из бэкенда
  const handleLoadStops = async (bbox: string) => {
    try {
      const res = await api.getStops(bbox, dummyConfig.stops);
      return res.items || [];
    } catch (err) {
      console.error('Failed to fetch stops by bbox', err);
      return [];
    }
  };

  return (
    <div className="relative w-screen h-screen overflow-hidden flex">
      {/* Боковая панель поиска и результатов (слева) */}
      <div className="absolute top-4 left-4 z-40 w-96 max-h-[calc(100vh-2rem)] flex flex-col gap-3 overflow-y-auto pointer-events-auto">
        <SearchPanel
          labelA={labelA}
          labelB={labelB}
          pointA={pointA}
          pointB={pointB}
          onSelectPlace={handleSelectPlace}
          onClearPoint={handleClearPoint}
          onSwapPoints={handleSwapPoints}
          onSearch={handleSearch}
          dummyConfig={dummyConfig}
          onUpdateDummyConfig={setDummyConfig}
        />

        <RouteResults
          routes={routes}
          selectedIndex={selectedRouteIndex}
          onSelectRoute={setSelectedRouteIndex}
          isLoading={isLoadingRoutes}
        />
      </div>

      {/* Окно выбранной остановки */}
      <StopDrawer
        stop={selectedStop}
        onClose={() => setSelectedStop(null)}
        dummyMode={dummyConfig.departures}
      />

      {/* Карта (зум и контролы автоматически перенесены вправо) */}
      <div className="w-full h-full">
        <YandexMap
          pointA={pointA}
          pointB={pointB}
          onPointChange={handlePointChange}
          routes={routes}
          selectedRouteIndex={selectedRouteIndex}
          onSelectStop={setSelectedStop}
          loadStops={handleLoadStops}
        />
      </div>
    </div>
  );
};
