import React, { useState, useEffect } from 'react';
import { api } from '../api/client';
import { Suggestion, TransportMode, TicketProfile, TicketProfileInfo, Point, DummyConfig } from '../types/api';

interface SearchPanelProps {
  labelA: string;
  labelB: string;
  pointA: Point | null;
  pointB: Point | null;
  onSelectPlace: (type: 'A' | 'B', point: Point, label: string) => void;
  onClearPoint: (type: 'A' | 'B') => void;
  onSwapPoints: () => void;
  onSearch: (filters: {
    modes: TransportMode[];
    lessWalking: boolean;
    ticketProfile: TicketProfile;
    departureAt: string | null;
    arrivalAt: string | null;
    maxTransfers: number | null;
  }) => void;
  dummyConfig: DummyConfig;
  onUpdateDummyConfig: (newConfig: DummyConfig) => void;
  onClearPoints: () => void;
  vehiclesEnabled: boolean;
  onToggleVehicles: () => void;
  showAllVehicles: boolean;
  onToggleShowAllVehicles: () => void;
}

const MODES: { id: TransportMode; label: string }[] = [
  { id: 'metro', label: 'Метро' },
  { id: 'bus', label: 'Автобус' },
  { id: 'tram', label: 'Трамвай' },
  { id: 'trolleybus', label: 'Троллейбус' },
  { id: 'rail', label: 'Электричка' },
];

// Ползунок пересадок: 0..7 — конкретное число, 8 — без ограничений.
const UNLIMITED_TRANSFERS = 8;

export const SearchPanel: React.FC<SearchPanelProps> = ({
  labelA,
  labelB,
  pointA,
  pointB,
  onSelectPlace,
  onClearPoint,
  onSwapPoints,
  onSearch,
  dummyConfig,
  onUpdateDummyConfig,
  onClearPoints,
  vehiclesEnabled,
  onToggleVehicles,
  showAllVehicles,
  onToggleShowAllVehicles
}) => {
  const [inputA, setInputA] = useState(labelA);
  const [inputB, setInputB] = useState(labelB);
  const [suggestionsA, setSuggestionsA] = useState<Suggestion[]>([]);
  const [suggestionsB, setSuggestionsB] = useState<Suggestion[]>([]);
  const [activeField, setActiveField] = useState<'A' | 'B' | null>(null);
  const [showDummyDetails, setShowDummyDetails] = useState(false);
  const [isCompact, setIsCompact] = useState(false);

  // Фильтры
  const [selectedModes, setSelectedModes] = useState<TransportMode[]>([
    'metro', 'bus', 'tram', 'trolleybus', 'rail'
  ]);
  const [lessWalking, setLessWalking] = useState(false);
  const [ticketProfile, setTicketProfile] = useState<TicketProfile>('bank_card');
  const [profiles, setProfiles] = useState<TicketProfileInfo[]>([]);
  const [arrivalTime, setArrivalTime] = useState('');
  const [departureTime, setDepartureTime] = useState('');
  const [maxTransfersOption, setMaxTransfersOption] = useState(UNLIMITED_TRANSFERS);

  useEffect(() => { setInputA(labelA); }, [labelA]);
  useEffect(() => { setInputB(labelB); }, [labelB]);

  useEffect(() => {
    api.getTicketProfiles().then((data) => {
      if (data?.length) setProfiles(data);
    }).catch(console.error);
  }, []);

  // Автодополнение А
  useEffect(() => {
    if (activeField !== 'A' || inputA.trim().length < 2) {
      setSuggestionsA([]);
      return;
    }
    const timer = setTimeout(() => {
      api.getSuggestions(inputA, dummyConfig.suggest)
        .then(res => setSuggestionsA(res.items || []))
        .catch(() => setSuggestionsA([]));
    }, 250);
    return () => clearTimeout(timer);
  }, [inputA, activeField, dummyConfig.suggest]);

  // Автодополнение Б
  useEffect(() => {
    if (activeField !== 'B' || inputB.trim().length < 2) {
      setSuggestionsB([]);
      return;
    }
    const timer = setTimeout(() => {
      api.getSuggestions(inputB, dummyConfig.suggest)
        .then(res => setSuggestionsB(res.items || []))
        .catch(() => setSuggestionsB([]));
    }, 250);
    return () => clearTimeout(timer);
  }, [inputB, activeField, dummyConfig.suggest]);

  const handleSelectSuggestion = async (type: 'A' | 'B', item: Suggestion) => {
    try {
      const geoRes = await api.geocode({ uri: item.uri }, dummyConfig.geocode);
      if (geoRes.items?.length) {
        const place = geoRes.items[0];
        onSelectPlace(type, { lat: place.lat, lon: place.lon }, item.title);
        if (type === 'A') {
          setInputA(item.title);
          setSuggestionsA([]);
        } else {
          setInputB(item.title);
          setSuggestionsB([]);
        }
      }
    } catch (err) {
      console.error('Failed to resolve suggestion', err);
    } finally {
      setActiveField(null);
    }
  };

  const handleClear = (type: 'A' | 'B') => {
    if (type === 'A') {
      setInputA('');
      setSuggestionsA([]);
    } else {
      setInputB('');
      setSuggestionsB([]);
    }
    onClearPoint(type);
  };

  const handleClearPoints = () => {
    setInputA('');
    setInputB('');
    setSuggestionsA([]);
    setSuggestionsB([]);
    onClearPoints();
  };

  // Время относится к сегодняшнему дню; если оно уже прошло,
  // планируем на завтра, чтобы запрос оставался в будущем.
  const buildTimeIso = (time: string): string | null => {
    const [hours, minutes] = time.split(':').map(Number);
    if (!Number.isInteger(hours) || !Number.isInteger(minutes)) return null;

    const value = new Date();
    value.setHours(hours, minutes, 0, 0);
    if (value.getTime() <= Date.now()) {
      value.setDate(value.getDate() + 1);
    }
    return value.toISOString();
  };

  const handleSearch = () => {
    onSearch({
      modes: selectedModes,
      lessWalking,
      ticketProfile,
      departureAt: departureTime ? buildTimeIso(departureTime) : null,
      arrivalAt: arrivalTime ? buildTimeIso(arrivalTime) : null,
      maxTransfers: maxTransfersOption === UNLIMITED_TRANSFERS ? null : maxTransfersOption,
    });
  };

  const transfersLabel = maxTransfersOption === UNLIMITED_TRANSFERS
    ? 'Без ограничений'
    : String(maxTransfersOption);

  const timeHint = departureTime
    ? 'Маршрут будет построен от указанного времени'
    : arrivalTime
      ? 'Маршрут будет построен к указанному времени'
      : 'Без времени — отправление сейчас';

  const toggleAllDummy = (enabled: boolean) => {
    onUpdateDummyConfig({
      suggest: enabled,
      geocode: enabled,
      routes: enabled,
      stops: enabled,
      departures: enabled,
      vehicles: enabled
    });
  };

  const isAllDummy = Object.values(dummyConfig).every(Boolean);

  return (
    <div className="bg-white p-3 sm:p-4 rounded-xl shadow-lg border border-slate-100 flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        {!isCompact && (
          <span className="text-sm font-semibold text-slate-700">Поиск маршрута</span>
        )}
        <button
          type="button"
          onClick={() => setIsCompact(prev => !prev)}
          aria-pressed={isCompact}
          className={`text-[11px] text-blue-600 hover:text-blue-800 underline ${isCompact ? 'ml-auto' : ''}`}
        >
          {isCompact ? 'Подробно' : 'Компактно'}
        </button>
      </div>

      {/* Панель настройки DUMMY режимов */}
      {!isCompact && (
      <div className="pb-2 border-b border-slate-100 text-xs">
        <div className="flex flex-wrap justify-between items-center gap-2">
          <label className="flex items-center gap-1.5 font-semibold text-slate-700 cursor-pointer">
            <input
              type="checkbox"
              checked={isAllDummy}
              onChange={(e) => toggleAllDummy(e.target.checked)}
              className="rounded text-blue-600 focus:ring-0 shrink-0"
            />
            <span>Все запросы Dummy (демо)</span>
          </label>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onToggleVehicles}
              aria-pressed={vehiclesEnabled}
              className={`text-[11px] px-2 py-1 rounded-md border transition ${
                vehiclesEnabled
                  ? 'bg-emerald-50 border-emerald-300 text-emerald-700'
                  : 'bg-white border-slate-200 text-slate-500 hover:text-slate-700'
              }`}
            >
              {vehiclesEnabled ? 'Скрыть транспорт' : 'Показать транспорт'}
            </button>
            <button
              type="button"
              onClick={() => setShowDummyDetails(prev => !prev)}
              className="text-blue-600 hover:text-blue-800 underline text-[11px]"
            >
              {showDummyDetails ? 'Скрыть эндпоинты' : 'Настроить эндпоинты'}
            </button>
          </div>
        </div>

        {showDummyDetails && (
          <div className="mt-2.5 p-2.5 bg-slate-50 rounded-lg grid grid-cols-1 gap-1.5 border border-slate-200">
            <label className="flex items-center justify-between gap-2 text-slate-600">
              <span className="truncate">/api/v1/suggest</span>
              <input
                type="checkbox"
                checked={dummyConfig.suggest}
                onChange={(e) => onUpdateDummyConfig({ ...dummyConfig, suggest: e.target.checked })}
                className="shrink-0"
              />
            </label>
            <label className="flex items-center justify-between gap-2 text-slate-600">
              <span className="truncate">/api/v1/geocode & reverse</span>
              <input
                type="checkbox"
                checked={dummyConfig.geocode}
                onChange={(e) => onUpdateDummyConfig({ ...dummyConfig, geocode: e.target.checked })}
                className="shrink-0"
              />
            </label>
            <label className="flex items-center justify-between gap-2 text-slate-600">
              <span className="truncate">/api/v1/routes (планировщик)</span>
              <input
                type="checkbox"
                checked={dummyConfig.routes}
                onChange={(e) => onUpdateDummyConfig({ ...dummyConfig, routes: e.target.checked })}
                className="shrink-0"
              />
            </label>
            <label className="flex items-center justify-between gap-2 text-slate-600">
              <span className="truncate">/api/v1/stops (остановки BBOX)</span>
              <input
                type="checkbox"
                checked={dummyConfig.stops}
                onChange={(e) => onUpdateDummyConfig({ ...dummyConfig, stops: e.target.checked })}
                className="shrink-0"
              />
            </label>
            <label className="flex items-center justify-between gap-2 text-slate-600">
              <span className="truncate">/api/v1/stops/.../departures</span>
              <input
                type="checkbox"
                checked={dummyConfig.departures}
                onChange={(e) => onUpdateDummyConfig({ ...dummyConfig, departures: e.target.checked })}
                className="shrink-0"
              />
            </label>
            <label className="flex items-center justify-between gap-2 text-slate-600">
              <span className="truncate">/api/v1/vehicles</span>
              <input
                type="checkbox"
                checked={dummyConfig.vehicles}
                onChange={(e) => onUpdateDummyConfig({ ...dummyConfig, vehicles: e.target.checked })}
                className="shrink-0"
              />
            </label>
          </div>
        )}
      </div>
      )}

      {/* Поля ввода адресов */}
      <div className="relative flex flex-col gap-2">
        {/* Точка А */}
        <div className="relative">
          <div className="flex items-center border border-slate-200 rounded-lg px-2.5 py-1.5 focus-within:border-blue-500">
            <span className="w-5 h-5 flex items-center justify-center bg-emerald-100 text-emerald-800 rounded-full text-xs font-bold mr-2">
              A
            </span>
            <input
              type="text"
              placeholder={pointA ? 'Точка А установлена' : 'Откуда (или кликните на карте)'}
              value={inputA}
              onChange={(e) => {
                setInputA(e.target.value);
                if (e.target.value === '') onClearPoint('A');
              }}
              onFocus={() => setActiveField('A')}
              className="w-full text-sm outline-none"
            />
            {inputA && (
              <button
                type="button"
                onClick={() => handleClear('A')}
                className="text-slate-400 hover:text-slate-600 px-1 font-bold text-xs"
              >
                ✕
              </button>
            )}
          </div>
          {suggestionsA.length > 0 && activeField === 'A' && (
            <ul className="absolute z-50 left-0 right-0 mt-1 bg-white border border-slate-200 rounded-lg shadow-xl max-h-52 overflow-auto">
              {suggestionsA.map((s, idx) => (
                <li
                  key={idx}
                  onClick={() => handleSelectSuggestion('A', s)}
                  className="px-3 py-2 hover:bg-slate-50 cursor-pointer border-b last:border-0"
                >
                  <p className="text-sm font-medium text-slate-800 break-words">{s.title}</p>
                  {s.subtitle && <p className="text-xs text-slate-500 break-words">{s.subtitle}</p>}
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Кнопки управления точками */}
        {!isCompact && (
          <div className="self-end mr-3 flex items-center gap-3 text-xs">
            <button
              type="button"
              onClick={handleClearPoints}
              title="Очистить точки А и Б"
              className="text-slate-400 hover:text-rose-600 flex items-center gap-1"
            >
              ✕ Очистить точки
            </button>
            <button
              type="button"
              onClick={onSwapPoints}
              title="Поменять местами"
              className="text-slate-400 hover:text-slate-700 flex items-center gap-1"
            >
              ⇅ Поменять местами
            </button>
          </div>
        )}

        {/* Точка Б */}
        <div className="relative">
          <div className="flex items-center border border-slate-200 rounded-lg px-2.5 py-1.5 focus-within:border-blue-500">
            <span className="w-5 h-5 flex items-center justify-center bg-rose-100 text-rose-800 rounded-full text-xs font-bold mr-2">
              Б
            </span>
            <input
              type="text"
              placeholder={pointB ? 'Точка Б установлена' : 'Куда (или кликните на карте)'}
              value={inputB}
              onChange={(e) => {
                setInputB(e.target.value);
                if (e.target.value === '') onClearPoint('B');
              }}
              onFocus={() => setActiveField('B')}
              className="w-full text-sm outline-none"
            />
            {inputB && (
              <button
                type="button"
                onClick={() => handleClear('B')}
                className="text-slate-400 hover:text-slate-600 px-1 font-bold text-xs"
              >
                ✕
              </button>
            )}
          </div>
          {suggestionsB.length > 0 && activeField === 'B' && (
            <ul className="absolute z-50 left-0 right-0 mt-1 bg-white border border-slate-200 rounded-lg shadow-xl max-h-52 overflow-auto">
              {suggestionsB.map((s, idx) => (
                <li
                  key={idx}
                  onClick={() => handleSelectSuggestion('B', s)}
                  className="px-3 py-2 hover:bg-slate-50 cursor-pointer border-b last:border-0"
                >
                  <p className="text-sm font-medium text-slate-800 break-words">{s.title}</p>
                  {s.subtitle && <p className="text-xs text-slate-500 break-words">{s.subtitle}</p>}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {/* Фильтры по видам транспорта */}
      {!isCompact && (
      <div className="flex flex-col gap-1">
        <label className="text-xs font-semibold text-slate-600">Виды транспорта:</label>
        <div className="flex flex-wrap gap-1">
          {MODES.map((m) => {
            const active = selectedModes.includes(m.id);
            return (
              <button
                key={m.id}
                type="button"
                onClick={() => {
                  setSelectedModes(prev =>
                    prev.includes(m.id) ? prev.filter(x => x !== m.id) : [...prev, m.id]
                  );
                }}
                className={`text-xs px-2 py-1 rounded-md border transition ${
                  active
                    ? 'bg-blue-50 border-blue-400 text-blue-700 font-medium'
                    : 'bg-white border-slate-200 text-slate-500'
                }`}
              >
                {m.label}
              </button>
            );
          })}
        </div>
      </div>
      )}

      {/* Проездной и «Меньше пешком» */}
      {!isCompact && (
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="min-w-0">
          <label className="font-semibold text-slate-600 block mb-1">Проездной:</label>
          <select
            value={ticketProfile}
            onChange={(e) => setTicketProfile(e.target.value as TicketProfile)}
            className="w-full min-w-0 border border-slate-200 rounded-md p-1.5 text-slate-700 outline-none"
          >
            {profiles.length > 0 ? (
              profiles.map(p => <option key={p.id} value={p.id}>{p.label_ru}</option>)
            ) : (
              <>
                <option value="bank_card">Банковская карта</option>
                <option value="podorozhnik_wallet">Подорожник</option>
                <option value="unlimited_all">Единый безлимит</option>
                <option value="unlimited_metro">Метро безлимит</option>
                <option value="unlimited_surface">Наземный безлимит</option>
              </>
            )}
          </select>
        </div>

        <div className="flex items-end pb-1.5 min-w-0">
          <label className="flex items-center gap-1.5 cursor-pointer text-slate-700">
            <input
              type="checkbox"
              checked={lessWalking}
              onChange={(e) => setLessWalking(e.target.checked)}
              className="rounded text-blue-600 focus:ring-0 shrink-0"
            />
            <span className="leading-tight">Меньше пешком</span>
          </label>
        </div>
      </div>
      )}

      {/* Время прибытия и пересадки */}
      {!isCompact && (
        <div className="flex flex-col gap-2 text-xs">
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label htmlFor="departure-time" className="font-semibold text-slate-600">Отправление в:</label>
              <input
                id="departure-time"
                type="time"
                value={departureTime}
                onChange={(e) => {
                  setDepartureTime(e.target.value);
                  if (e.target.value) setArrivalTime('');
                }}
                className="w-full border border-slate-200 rounded-md p-1.5 text-slate-700 outline-none"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor="arrival-time" className="font-semibold text-slate-600">Прибытие к:</label>
              <input
                id="arrival-time"
                type="time"
                value={arrivalTime}
                onChange={(e) => {
                  setArrivalTime(e.target.value);
                  if (e.target.value) setDepartureTime('');
                }}
                className="w-full border border-slate-200 rounded-md p-1.5 text-slate-700 outline-none"
              />
            </div>
          </div>
          <div className="flex items-center justify-between gap-2">
            <p className="text-[11px] text-slate-400">{timeHint}</p>
            {(departureTime || arrivalTime) && (
              <button
                type="button"
                onClick={() => {
                  setDepartureTime('');
                  setArrivalTime('');
                }}
                className="text-[11px] text-slate-400 hover:text-slate-700 shrink-0"
              >
                Сбросить
              </button>
            )}
          </div>

          <div className="flex items-center justify-between gap-2">
            <label htmlFor="max-transfers" className="font-semibold text-slate-600">Пересадки:</label>
            <span className="font-semibold text-slate-700">{transfersLabel}</span>
          </div>
          <input
            id="max-transfers"
            type="range"
            min={0}
            max={UNLIMITED_TRANSFERS}
            step={1}
            value={maxTransfersOption}
            onChange={(e) => setMaxTransfersOption(Number(e.target.value))}
            className="w-full accent-blue-600"
            aria-valuetext={transfersLabel}
          />
        </div>
      )}

      <button
        type="button"
        disabled={!pointA || !pointB}
        onClick={handleSearch}
        className="w-full bg-blue-600 hover:bg-blue-700 disabled:bg-slate-200 disabled:text-slate-400 text-white font-medium py-2 rounded-lg transition"
      >
        {!pointA ? 'Укажите точку А' : !pointB ? 'Укажите точку Б' : 'Найти маршруты'}
      </button>

      {!isCompact && vehiclesEnabled && (
        <button
          type="button"
          onClick={onToggleShowAllVehicles}
          aria-pressed={showAllVehicles}
          className="w-full text-xs font-medium py-2 rounded-lg border border-slate-300 text-slate-700 bg-white hover:bg-slate-50 transition"
        >
          {showAllVehicles ? 'Только транспорт выбранного маршрута' : 'Показать весь транспорт'}
        </button>
      )}
    </div>
  );
};
