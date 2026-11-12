import {
  SuggestResponse,
  GeocodeResponse,
  RouteRequest,
  RoutesResponse,
  GtfsStopsResponse,
  GtfsDeparturesResponse,
  TicketProfileInfo
} from '../types/api';

const API_BASE = import.meta.env.VITE_API_URL || '';

export const api = {
  // Подсказки при вводе адреса
  async getSuggestions(q: string, dummy = true): Promise<SuggestResponse> {
    const params = new URLSearchParams({ q, dummy: String(dummy), provider: 'yandex', limit: '5' });
    const res = await fetch(`${API_BASE}/api/v1/suggest?${params}`);
    if (!res.ok) throw new Error(`Suggest failed: ${res.statusText}`);
    return res.json();
  },

  // Геокодирование по тексту или по URI подсказки
  async geocode(query: { q?: string; uri?: string }, dummy = true): Promise<GeocodeResponse> {
    const params = new URLSearchParams({ dummy: String(dummy), provider: 'yandex' });
    if (query.q) params.append('q', query.q);
    if (query.uri) params.append('uri', query.uri);

    const res = await fetch(`${API_BASE}/api/v1/geocode?${params}`);
    if (!res.ok) throw new Error(`Geocode failed: ${res.statusText}`);
    return res.json();
  },

  // Обратное геокодирование при клике или перетаскивании маркера на карте
  async reverseGeocode(lat: number, lon: number, dummy = true): Promise<GeocodeResponse> {
    const params = new URLSearchParams({
      lat: lat.toString(),
      lon: lon.toString(),
      dummy: String(dummy),
      provider: 'yandex'
    });
    const res = await fetch(`${API_BASE}/api/v1/reverse?${params}`);
    if (!res.ok) throw new Error(`Reverse geocode failed: ${res.statusText}`);
    return res.json();
  },

  // Построение маршрутов
  async planRoute(payload: RouteRequest, dummy = true): Promise<RoutesResponse> {
    const res = await fetch(`${API_BASE}/api/v1/routes?dummy=${dummy}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`Planning failed: ${res.statusText}`);
    return res.json();
  },

  // Список остановок в видимой области (BBOX)
  async getStops(bbox: string, dummy = true): Promise<GtfsStopsResponse> {
    const params = new URLSearchParams({ bbox, dummy: String(dummy), limit: '200' });
    const res = await fetch(`${API_BASE}/api/v1/stops?${params}`);
    if (!res.ok) throw new Error(`Stops load failed: ${res.statusText}`);
    return res.json();
  },

  // Расписание и отправления с остановки
  async getStopDepartures(stopId: string, departureAt: string, dummy = true): Promise<GtfsDeparturesResponse> {
    const params = new URLSearchParams({
      departure_at: departureAt,
      dummy: String(dummy),
      limit: '20'
    });
    const res = await fetch(`${API_BASE}/api/v1/stops/${encodeURIComponent(stopId)}/departures?${params}`);
    if (!res.ok) throw new Error(`Departures failed: ${res.statusText}`);
    return res.json();
  },

  // Справочник типов проездных
  async getTicketProfiles(): Promise<TicketProfileInfo[]> {
    const res = await fetch(`${API_BASE}/api/v1/ticket-profiles`);
    if (!res.ok) return [];
    return res.json();
  }
};