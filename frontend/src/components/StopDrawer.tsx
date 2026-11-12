import React, { useEffect, useState } from 'react';
import { GtfsStop, GtfsDeparture } from '../types/api';
import { api } from '../api/client';

interface StopDrawerProps {
  stop: GtfsStop | null;
  onClose: () => void;
  dummyMode: boolean;
}

export const StopDrawer: React.FC<StopDrawerProps> = ({ stop, onClose, dummyMode }) => {
  const [departures, setDepartures] = useState<GtfsDeparture[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!stop) return;
    setLoading(true);
    const nowIso = new Date().toISOString();

    api.getStopDepartures(stop.stop_id, nowIso, dummyMode)
      .then((res) => setDepartures(res.items || []))
      .catch((err) => {
        console.error('Failed to load departures', err);
        setDepartures([]);
      })
      .finally(() => setLoading(false));
  }, [stop, dummyMode]);

  if (!stop) return null;

  return (
    <div className="absolute z-50 bottom-2 left-2 right-2 w-auto max-h-[70vh] bg-white rounded-xl shadow-2xl border border-slate-200 overflow-hidden flex flex-col sm:bottom-6 sm:left-auto sm:right-6 sm:w-80 sm:max-h-[480px]">
      <div className="p-4 bg-slate-900 text-white flex justify-between items-start">
        <div>
          <h4 className="font-bold text-base leading-tight">{stop.name}</h4>
          {stop.description && <p className="text-xs text-slate-400 mt-1">{stop.description}</p>}
        </div>
        <button
          onClick={onClose}
          className="text-slate-400 hover:text-white text-lg font-bold p-1 leading-none"
        >
          ✕
        </button>
      </div>

      <div className="p-4 overflow-y-auto flex-1">
        <h5 className="text-xs font-semibold uppercase tracking-wider text-slate-500 mb-2">
          Ближайшие рейсы:
        </h5>

        {loading ? (
          <p className="text-xs text-slate-500">Загрузка расписания...</p>
        ) : departures.length > 0 ? (
          <div className="flex flex-col divide-y divide-slate-100">
            {departures.map((dep, idx) => (
              <div key={idx} className="py-2 flex items-center justify-between text-xs">
                <div className="flex items-center gap-2">
                  <span
                    className="font-bold px-1.5 py-0.5 rounded text-white"
                    style={{ backgroundColor: dep.line.color ? `#${dep.line.color}` : '#2563EB' }}
                  >
                    {dep.line.short_name}
                  </span>
                  <span className="text-slate-700 truncate max-w-[120px]">
                    {dep.headsign || 'По маршруту'}
                  </span>
                </div>

                <div className="text-right font-medium text-slate-800">
                  {dep.schedule_type === 'frequency' && dep.headway_seconds ? (
                    <span className="text-slate-500">каждые {Math.round(dep.headway_seconds / 60)} мин</span>
                  ) : dep.scheduled_departure_at ? (
                    new Date(dep.scheduled_departure_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                  ) : (
                    dep.departure_time || '—'
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-xs text-slate-400">Нет отправлений на ближайшее время</p>
        )}
      </div>
    </div>
  );
};