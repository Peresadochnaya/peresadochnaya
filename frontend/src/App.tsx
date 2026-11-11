import React, { useState } from 'react';
import { YandexMap } from './components/YandexMap';
import { SearchPanel } from './components/SearchPanel';
import { RouteResults } from './components/RouteResults';
import { StopDrawer } from './components/StopDrawer';
import { Point, Route, GtfsStop, TransportMode, TicketProfile } from './types/api';
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
  const [dummyMode, setDummyMode] = useState(true);

  // Обработка установки точек (клик/драг на карте)
  const handlePointChange = async (type: 'A' | 'B', point: Point) => {
    if (type === 'A') setPointA(point);
    else setPointB(point);

    // Вызов обратного геокодирования для заполнения понятного адреса в инпуте
    try {
      const res = await api.reverseGeocode(point.lat, point.lon, dummyMode);
      const text = res.items?.[0]?.label || `${point.lat.toFixed(4)}, ${point.lon.toFixed(4)}`;
      if (type === 'A') setLabelA(text);
      else setLabelB(text);
    } catch {
      const fallback = `${point.lat.toFixed(4)}, ${point.lon.toFixed(4)}`;
      if (type === 'A') setLabelA(fallback);
      else setLabelB(fallback);
    }
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
      }, dummyMode);

      setRoutes(res.routes || []);
      setSelectedRouteIndex(0);
    } catch (err) {
      console.error('Route planning error', err);
      alert('Не удалось построить маршрут. Включите режим Демо (dummy), если бэкенд OTP еще не запущен.');
    } finally {
      setIsLoadingRoutes(false);
    }
  };

  const handleLoadStops = async (bbox: string) => {
    try {
      const res = await api.getStops(bbox, dummyMode);
      return res.items || [];
    } catch (err) {
      console.error('Failed to fetch stops by bbox', err);
      return [];
    }
  };

  return (
    <div className="relative w-screen h-screen overflow-hidden flex">
      {/* Боковая панель поиска и результатов */}
      <div className="absolute top-4 left-4 z-40 w-96 max-h-[calc(100vh-2rem)] flex flex-col gap-4 overflow-y-auto no-scrollbar pointer-events-auto">
        <SearchPanel
          labelA={labelA}
          labelB={labelB}
          pointA={pointA}
          pointB={pointB}
          onSelectPlace={handleSelectPlace}
          onSwapPoints={handleSwapPoints}
          onSearch={handleSearch}
          dummyMode={dummyMode}
          onToggleDummy={setDummyMode}
        />

        <RouteResults
          routes={routes}
          selectedIndex={selectedRouteIndex}
          onSelectRoute={setSelectedRouteIndex}
          isLoading={isLoadingRoutes}
        />
      </div>

      {/* Окно остановки */}
      <StopDrawer
        stop={selectedStop}
        onClose={() => setSelectedStop(null)}
        dummyMode={dummyMode}
      />

      {/* Карта */}
      <div className="w-full h-full">
        <YandexMap
          pointA={pointA}
          pointB={pointB}
          onPointChange={handlePointChange}
          routes={routes}
          selectedRouteIndex={selectedRouteIndex}
          onSelectStop={setSelectedStop}
          dummyMode={dummyMode}
          loadStops={handleLoadStops}
        />
      </div>
    </div>
  );
};