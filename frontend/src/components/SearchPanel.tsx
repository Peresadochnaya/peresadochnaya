import React, { useState, useEffect } from 'react';
import { api } from '../api/client';
import { Suggestion, TransportMode, TicketProfile, TicketProfileInfo, Point } from '../types/api';

interface SearchPanelProps {
  labelA: string;
  labelB: string;
  pointA: Point | null;
  pointB: Point | null;
  onSelectPlace: (type: 'A' | 'B', point: Point, label: string) => void;
  onSwapPoints: () => void;
  onSearch: (filters: {
    modes: TransportMode[];
    lessWalking: boolean;
    ticketProfile: TicketProfile;
  }) => void;
  dummyMode: boolean;
  onToggleDummy: (val: boolean) => void;
}

const MODES: { id: TransportMode; label: string }[] = [
  { id: 'metro', label: 'Метро' },
  { id: 'bus', label: 'Автобус' },
  { id: 'tram', label: 'Трамвай' },
  { id: 'trolleybus', label: 'Троллейбус' },
  { id: 'rail', label: 'Электричка' },
];

export const SearchPanel: React.FC<SearchPanelProps> = ({
  labelA,
  labelB,
  pointA,
  pointB,
  onSelectPlace,
  onSwapPoints,
  onSearch,
  dummyMode,
  onToggleDummy
}) => {
  const [inputA, setInputA] = useState(labelA);
  const [inputB, setInputB] = useState(labelB);
  const [suggestionsA, setSuggestionsA] = useState<Suggestion[]>([]);
  const [suggestionsB, setSuggestionsB] = useState<Suggestion[]>([]);
  const [activeField, setActiveField] = useState<'A' | 'B' | null>(null);

  // Фильтры поиска
  const [selectedModes, setSelectedModes] = useState<TransportMode[]>([
    'metro', 'bus', 'tram', 'trolleybus', 'rail'
  ]);
  const [lessWalking, setLessWalking] = useState(false);
  const [ticketProfile, setTicketProfile] = useState<TicketProfile>('bank_card');
  const [profiles, setProfiles] = useState<TicketProfileInfo[]>([]);

  useEffect(() => { setInputA(labelA); }, [labelA]);
  useEffect(() => { setInputB(labelB); }, [labelB]);

  // Загрузка доступных типов проездных
  useEffect(() => {
    api.getTicketProfiles().then((data) => {
      if (data?.length) setProfiles(data);
    }).catch(console.error);
  }, []);

  // Автодополнение с дебаунсом
  useEffect(() => {
    if (activeField !== 'A' || inputA.trim().length < 2) {
      setSuggestionsA([]);
      return;
    }
    const timer = setTimeout(() => {
      api.getSuggestions(inputA, dummyMode)
        .then(res => setSuggestionsA(res.items || []))
        .catch(() => setSuggestionsA([]));
    }, 250);
    return () => clearTimeout(timer);
  }, [inputA, activeField, dummyMode]);

  useEffect(() => {
    if (activeField !== 'B' || inputB.trim().length < 2) {
      setSuggestionsB([]);
      return;
    }
    const timer = setTimeout(() => {
      api.getSuggestions(inputB, dummyMode)
        .then(res => setSuggestionsB(res.items || []))
        .catch(() => setSuggestionsB([]));
    }, 250);
    return () => clearTimeout(timer);
  }, [inputB, activeField, dummyMode]);

  const handleSelectSuggestion = async (type: 'A' | 'B', item: Suggestion) => {
    try {
      const geoRes = await api.geocode({ uri: item.uri }, dummyMode);
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
      console.error('Failed to resolve suggestion coords', err);
    } finally {
      setActiveField(null);
    }
  };

  const toggleMode = (mode: TransportMode) => {
    setSelectedModes(prev =>
      prev.includes(mode) ? prev.filter(m => m !== mode) : [...prev, mode]
    );
  };

  return (
    <div className="bg-white p-4 rounded-xl shadow-lg border border-slate-100 flex flex-col gap-4">
      {/* Переключатель Dummy / Real API */}
      <div className="flex justify-between items-center text-xs text-slate-500 pb-2 border-b">
        <span>Режим API:</span>
        <label className="flex items-center gap-1 cursor-pointer">
          <input
            type="checkbox"
            checked={dummyMode}
            onChange={(e) => onToggleDummy(e.target.checked)}
            className="rounded text-blue-600 focus:ring-0"
          />
          <span>Демо (dummy)</span>
        </label>
      </div>

      {/* Поля ввода адресов */}
      <div className="relative flex flex-col gap-2">
        {/* Точка А */}
        <div className="relative">
          <div className="flex items-center border border-slate-200 rounded-lg px-3 py-2 focus-within:border-blue-500">
            <span className="w-6 h-6 flex items-center justify-center bg-emerald-100 text-emerald-800 rounded-full text-xs font-bold mr-2">
              A
            </span>
            <input
              type="text"
              placeholder="Откуда (или кликните на карте)"
              value={inputA}
              onChange={(e) => setInputA(e.target.value)}
              onFocus={() => setActiveField('A')}
              className="w-full text-sm outline-none"
            />
          </div>
          {suggestionsA.length > 0 && activeField === 'A' && (
            <ul className="absolute z-50 left-0 right-0 mt-1 bg-white border border-slate-200 rounded-lg shadow-xl max-h-56 overflow-auto">
              {suggestionsA.map((s, idx) => (
                <li
                  key={idx}
                  onClick={() => handleSelectSuggestion('A', s)}
                  className="px-3 py-2 hover:bg-slate-50 cursor-pointer border-b last:border-0"
                >
                  <p className="text-sm font-medium text-slate-800">{s.title}</p>
                  {s.subtitle && <p className="text-xs text-slate-500">{s.subtitle}</p>}
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Кнопка смены мест точек */}
        <button
          onClick={onSwapPoints}
          title="Поменять точки местами"
          className="self-end mr-4 text-slate-400 hover:text-slate-700 text-xs flex items-center gap-1 transition"
        >
          ⇅ Поменять местами
        </button>

        {/* Точка Б */}
        <div className="relative">
          <div className="flex items-center border border-slate-200 rounded-lg px-3 py-2 focus-within:border-blue-500">
            <span className="w-6 h-6 flex items-center justify-center bg-rose-100 text-rose-800 rounded-full text-xs font-bold mr-2">
              Б
            </span>
            <input
              type="text"
              placeholder="Куда (или кликните на карте)"
              value={inputB}
              onChange={(e) => setInputB(e.target.value)}
              onFocus={() => setActiveField('B')}
              className="w-full text-sm outline-none"
            />
          </div>
          {suggestionsB.length > 0 && activeField === 'B' && (
            <ul className="absolute z-50 left-0 right-0 mt-1 bg-white border border-slate-200 rounded-lg shadow-xl max-h-56 overflow-auto">
              {suggestionsB.map((s, idx) => (
                <li
                  key={idx}
                  onClick={() => handleSelectSuggestion('B', s)}
                  className="px-3 py-2 hover:bg-slate-50 cursor-pointer border-b last:border-0"
                >
                  <p className="text-sm font-medium text-slate-800">{s.title}</p>
                  {s.subtitle && <p className="text-xs text-slate-500">{s.subtitle}</p>}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {/* Фильтры: Виды транспорта */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-semibold text-slate-600">Виды транспорта:</label>
        <div className="flex flex-wrap gap-1">
          {MODES.map((m) => {
            const active = selectedModes.includes(m.id);
            return (
              <button
                key={m.id}
                type="button"
                onClick={() => toggleMode(m.id)}
                className={`text-xs px-2.5 py-1 rounded-md border transition ${
                  active
                    ? 'bg-blue-50 border-blue-400 text-blue-700 font-medium'
                    : 'bg-white border-slate-200 text-slate-500 hover:border-slate-300'
                }`}
              >
                {m.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Фильтры: Пешая доступность и Проездной */}
      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <label className="font-semibold text-slate-600 block mb-1">Проездной:</label>
          <select
            value={ticketProfile}
            onChange={(e) => setTicketProfile(e.target.value as TicketProfile)}
            className="w-full border border-slate-200 rounded-md p-1.5 text-slate-700 outline-none"
          >
            {profiles.length > 0 ? (
              profiles.map(p => (
                <option key={p.id} value={p.id}>{p.label_ru}</option>
              ))
            ) : (
              <>
                <option value="bank_card">Банковская карта</option>
                <option value="podorozhnik_wallet">Подорожник (Кошелек)</option>
                <option value="unlimited_all">Единый безлимитный</option>
                <option value="unlimited_metro">Метро безлимит</option>
                <option value="unlimited_surface">Наземный безлимит</option>
              </>
            )}
          </select>
        </div>

        <div className="flex items-end pb-1.5">
          <label className="flex items-center gap-1.5 cursor-pointer text-slate-700">
            <input
              type="checkbox"
              checked={lessWalking}
              onChange={(e) => setLessWalking(e.target.checked)}
              className="rounded text-blue-600 focus:ring-0"
            />
            <span>Меньше пешком</span>
          </label>
        </div>
      </div>

      {/* Кнопка запуска поиска */}
      <button
        disabled={!pointA || !pointB}
        onClick={() => onSearch({ modes: selectedModes, lessWalking, ticketProfile })}
        className="w-full bg-blue-600 hover:bg-blue-700 disabled:bg-slate-200 disabled:text-slate-400 text-white font-medium py-2 rounded-lg transition"
      >
        Найти маршруты
      </button>
    </div>
  );
};