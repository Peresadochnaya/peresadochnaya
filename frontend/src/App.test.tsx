import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { App } from './App';
import { Point, VehiclePosition } from './types/api';

vi.mock('./components/YandexMap', () => ({
  YandexMap: ({
    onPointChange,
    vehicles,
  }: {
    onPointChange: (type: 'A' | 'B', point: Point) => void;
    vehicles: VehiclePosition[];
  }) => (
    <>
      <button onClick={() => onPointChange('A', { lat: 59.93, lon: 30.31 })}>Set A</button>
      <button onClick={() => onPointChange('B', { lat: 59.94, lon: 30.32 })}>Set B</button>
      <div data-testid="map-vehicles">{vehicles.map((v) => v.vehicle_id).join(',')}</div>
    </>
  ),
}));
const modeLabels = ['Метро', 'Автобус', 'Трамвай', 'Троллейбус', 'Электричка'];
const fetchMock = vi.fn();
const response = (body: unknown) => ({ ok: true, json: async () => body });
const noRoutes = /Маршруты не найдены/;

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url: string) => {
    if (url.includes('/ticket-profiles')) return response([]);
    if (url.includes('/reverse')) return response({ items: [] });
    if (url.includes('/routes')) return response({ mode: 'dummy', routes: [] });
    throw new Error(`Unexpected request: ${url}`);
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function setupPoints() {
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: 'Set A' }));
  fireEvent.click(screen.getByRole('button', { name: 'Set B' }));
  await screen.findByDisplayValue('59.9300, 30.3100');
  await screen.findByDisplayValue('59.9400, 30.3200');
}
async function submittedModes() {
  fireEvent.click(screen.getByRole('button', { name: 'Найти маршруты' }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url.includes('/routes'))).toBe(true));
  const calls = fetchMock.mock.calls.filter(([url]) => url.includes('/routes'));
  return JSON.parse(calls[calls.length - 1][1].body).modes;
}
describe('App routing filters over HTTP', () => {
  it('requests walking only when all transit is off, including compact search', async () => {
    await setupPoints();
    for (const name of modeLabels) fireEvent.click(screen.getByRole('button', { name }));
    fireEvent.click(screen.getByRole('button', { name: 'Компактно' }));
    expect(await submittedModes()).toEqual(['walk']);
  });
  it.each(modeLabels)('preserves selected %s without enabling other transit', async selected => {
    await setupPoints();
    for (const name of modeLabels.filter(name => name !== selected)) {
      fireEvent.click(screen.getByRole('button', { name }));
    }
    const ids: Record<string, string> = {
      Метро: 'metro', Автобус: 'bus', Трамвай: 'tram', Троллейбус: 'trolleybus', Электричка: 'rail',
    };
    expect(await submittedModes()).toEqual([ids[selected]]);
  });
  it('keeps all explicitly selected modes on initial search', async () => {
    await setupPoints();
    expect(await submittedModes()).toEqual(['metro', 'bus', 'tram', 'trolleybus', 'rail']);
  });
  it('sends the uncovered payment chosen for an unlimited pass', async () => {
    await setupPoints();
    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'unlimited_surface' } });
    fireEvent.change(screen.getByLabelText('Оплата вне безлимита:'), {
      target: { value: 'podorozhnik_wallet' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Найти маршруты' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url.includes('/routes'))).toBe(true));

    const calls = fetchMock.mock.calls.filter(([url]) => url.includes('/routes'));
    const body = JSON.parse(calls[calls.length - 1][1].body);
    expect(body.passenger).toEqual({
      ticket_profile: 'unlimited_surface',
      uncovered_payment: 'podorozhnik_wallet',
    });
  });
});
describe('App vehicles on the map', () => {
  const routeFixture = {
    id: 'route-metro-2',
    name: 'Метро 2',
    distance_meters: 1000,
    geometry: {
      type: 'LineString',
      coordinates: [[30.31, 59.93], [30.32, 59.94]],
    },
    start_at: '2026-11-13T10:00:00+03:00',
    end_at: '2026-11-13T10:10:00+03:00',
    duration_seconds: 600,
    transfers: 0,
    legs: [
      {
        mode: 'metro',
        from: { lat: 59.93, lon: 30.31, name: 'A' },
        to: { lat: 59.94, lon: 30.32, name: 'B' },
        start_at: '2026-11-13T10:00:00+03:00',
        end_at: '2026-11-13T10:10:00+03:00',
        duration_seconds: 600,
        original_duration_seconds: 600,
        duration_source: 'schedule',
        line: { short_name: '2', route_id: '1:metro:2' },
        geometry: {
          type: 'LineString',
          coordinates: [[30.31, 59.93], [30.32, 59.94]],
        },
        distance_meters: 1000,
      },
    ],
    provenance: { candidate_providers: [], traffic_providers: [] },
  };

  const vehicleFixture = (patch: Partial<VehiclePosition>): VehiclePosition => ({
    vehicle_id: 'v1',
    route_id: 'metro:2',
    trip_id: 'trip-1',
    lat: 59.93,
    lon: 30.31,
    mode: 'metro',
    timestamp: '2026-11-13T10:00:00+03:00',
    ...patch,
  });

  const mockRoutesAndVehicles = () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/ticket-profiles')) return response([]);
      if (url.includes('/reverse')) return response({ items: [] });
      if (url.includes('/vehicles')) {
        return response({
          mode: 'dummy',
          items: [
            vehicleFixture({ vehicle_id: 'selected-route', route_id: 'metro:2' }),
            vehicleFixture({ vehicle_id: 'other-route', route_id: 'metro:9', trip_id: 'trip-9' }),
          ],
          updated_at: '2026-11-13T10:00:00+03:00',
          stale: false,
          warnings: [],
        });
      }
      if (url.includes('/routes')) return response({ mode: 'dummy', routes: [routeFixture] });
      throw new Error(`Unexpected request: ${url}`);
    });
  };

  const mapVehicles = () => screen.getByTestId('map-vehicles').textContent;

  it('requests vehicles only after transport is enabled and shows selected route vehicles', async () => {
    mockRoutesAndVehicles();
    await setupPoints();

    fireEvent.click(screen.getByRole('button', { name: 'Найти маршруты' }));
    await screen.findByText('Предложенные маршруты (1):');
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/vehicles'))).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Показать транспорт' }));

    await waitFor(() => expect(mapVehicles()).toContain('selected-route'));
    expect(mapVehicles()).not.toContain('other-route');

    const vehiclesCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/vehicles'));
    expect(String(vehiclesCall![0])).toContain('dummy=true');
    expect(String(vehiclesCall![0])).toContain('bbox=');
  });

  it('shows all vehicles after the show-all toggle is pressed', async () => {
    mockRoutesAndVehicles();
    await setupPoints();
    fireEvent.click(screen.getByRole('button', { name: 'Найти маршруты' }));
    await screen.findByText('Предложенные маршруты (1):');
    fireEvent.click(screen.getByRole('button', { name: 'Показать транспорт' }));
    await waitFor(() => expect(mapVehicles()).toContain('selected-route'));

    fireEvent.click(screen.getByRole('button', { name: 'Показать весь транспорт' }));

    await waitFor(() => {
      expect(mapVehicles()).toContain('selected-route');
      expect(mapVehicles()).toContain('other-route');
    });
  });
});

describe('App empty search result', () => {
  it('appears only after a successful empty response and clears with points', async () => {
    await setupPoints();
    expect(screen.queryByText(noRoutes)).toBeNull();
    await submittedModes();
    expect(await screen.findByText(noRoutes)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /Очистить точки/ }));
    expect(screen.queryByText(noRoutes)).toBeNull();
  });
  it('does not confuse loading or request failure with no routes', async () => {
    await setupPoints();
    await submittedModes();
    await screen.findByText(noRoutes);
    let rejectRequest!: (error: Error) => void;
    fetchMock.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectRequest = reject; }));
    vi.spyOn(window, 'alert').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
    fireEvent.click(screen.getByRole('button', { name: 'Найти маршруты' }));
    expect(screen.getByText(/Ищем наилучшие/)).toBeTruthy();
    expect(screen.queryByText(noRoutes)).toBeNull();
    rejectRequest(new Error('Planner unavailable'));
    await waitFor(() => expect(window.alert).toHaveBeenCalledOnce());
    expect(screen.queryByText(noRoutes)).toBeNull();
    expect(screen.queryByText(/Ищем наилучшие/)).toBeNull();
  });
});
