import React from 'react';
import { Route, Leg } from '../types/api';

interface RouteResultsProps {
  routes: Route[];
  selectedIndex: number;
  onSelectRoute: (index: number) => void;
  isLoading: boolean;
}

const formatDuration = (seconds: number) => {
  const totalMinutes = Math.round(seconds / 60);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;

  if (hours === 0) return `${minutes} мин`;
  if (minutes === 0) return `${hours} ч`;
  return `${hours} ч ${minutes} мин`;
};

const formatTime = (isoString: string) => {
  const date = new Date(isoString);
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
};

const formatPrice = (amountRub: number) => {
  const rounded = Math.round(amountRub * 100) / 100;
  const text = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(2);
  return `${text} ₽`;
};

const getModeBadge = (leg: Leg) => {
  switch (leg.mode) {
    case 'walk':
      return <span className="bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded text-[11px]">Пешком {formatDuration(leg.duration_seconds)}</span>;
    case 'wait':
      return <span className="bg-amber-50 text-amber-600 px-1.5 py-0.5 rounded text-[11px]">Ожидание {formatDuration(leg.duration_seconds)}</span>;
    default:
      return (
        <span className="bg-blue-100 text-blue-800 font-semibold px-1.5 py-0.5 rounded text-[11px] flex items-center gap-1">
          {leg.mode.toUpperCase()} {leg.line?.short_name}
        </span>
      );
  }
};

export const RouteResults: React.FC<RouteResultsProps> = ({
  routes,
  selectedIndex,
  onSelectRoute,
  isLoading
}) => {
  if (isLoading) {
    return (
      <div className="bg-white p-6 rounded-xl shadow border border-slate-100 text-center text-slate-500 text-sm">
        Ищем наилучшие варианты проезда...
      </div>
    );
  }

  if (!routes.length) {
    return null;
  }

  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold text-slate-700 px-1">
        Предложенные маршруты ({routes.length}):
      </h3>

      {routes.map((route, idx) => {
        const isSelected = idx === selectedIndex;

        return (
          <div
            key={route.id || idx}
            onClick={() => onSelectRoute(idx)}
            className={`cursor-pointer p-4 rounded-xl border transition flex flex-col gap-2 ${
              isSelected
                ? 'bg-blue-50 border-blue-500 ring-2 ring-blue-500 shadow-md'
                : 'bg-white border-slate-200 hover:border-slate-300 shadow-sm'
            }`}
          >
            {/* Заголовок карточки: время и длительность */}
            <div className="flex justify-between items-baseline gap-2">
              <span className="text-xl font-bold text-slate-900">
                {formatDuration(route.duration_seconds)}
              </span>
              <span className="text-xs text-slate-500 font-medium shrink-0">
                {formatTime(route.start_at)} — {formatTime(route.end_at)}
              </span>
            </div>

            {/* Мета-информация */}
            <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
              <span>Пересадок: {route.transfers}</span>
              <span>•</span>
              <span>{(route.distance_meters / 1000).toFixed(1)} км</span>
              {route.fare && (
                <>
                  <span>•</span>
                  <span className="font-semibold text-emerald-700">
                    {formatPrice(route.fare.amount_rub)}
                  </span>
                </>
              )}
            </div>

            {/* Визуальная цепочка участков (Legs) */}
            <div className="flex flex-wrap items-center gap-1.5 pt-1">
              {route.legs.map((leg, lIdx) => (
                <React.Fragment key={lIdx}>
                  {getModeBadge(leg)}
                  {lIdx < route.legs.length - 1 && <span className="text-slate-300 text-xs">›</span>}
                </React.Fragment>
              ))}
            </div>

            {/* Подробный список остановок перегона при выбранном маршруте */}
            {isSelected && (
              <div className="mt-3 pt-3 border-t border-slate-200 flex flex-col gap-2 text-xs">
                {route.legs.map((leg, lIdx) => {
                  if (leg.mode === 'wait') {
                    return (
                      <div key={lIdx} className="flex gap-2">
                        <div className="w-1.5 bg-amber-300 rounded-full" />
                        <div className="flex-1 flex justify-between gap-2 font-medium text-amber-700">
                          <span>Ожидание</span>
                          <span className="shrink-0">{formatTime(leg.start_at)}</span>
                        </div>
                      </div>
                    );
                  }

                  return (
                    <div key={lIdx} className="flex gap-2">
                      <div className="w-1.5 bg-slate-300 rounded-full" />
                      <div className="flex-1">
                        <div className="flex justify-between gap-2 font-medium text-slate-700">
                          <span className="min-w-0 truncate">{leg.from.name || 'Точка отправления'}</span>
                          <span className="shrink-0">{formatTime(leg.start_at)}</span>
                        </div>
                        <div className="text-slate-500 my-0.5">
                          {leg.line ? `${leg.mode}: ${leg.line.short_name}` : leg.mode} ({formatDuration(leg.duration_seconds)})
                        </div>
                        <div className="flex justify-between gap-2 font-medium text-slate-700">
                          <span className="min-w-0 truncate">{leg.to.name || 'Точка прибытия'}</span>
                          <span className="shrink-0">{formatTime(leg.end_at)}</span>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};