import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { App } from './App';
import { Point } from './types/api';

vi.mock('./components/YandexMap', () => ({
  YandexMap: ({ onPointChange }: { onPointChange: (type: 'A' | 'B', point: Point) => void }) => (
    <>
      <button onClick={() => onPointChange('A', { lat: 59.93, lon: 30.31 })}>Set A</button>
      <button onClick={() => onPointChange('B', { lat: 59.94, lon: 30.32 })}>Set B</button>
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
