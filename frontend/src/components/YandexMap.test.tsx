import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render } from '@testing-library/react';
import { YandexMap } from './YandexMap';
import { Point, Route } from '../types/api';

type MapClickHandler = (event: { get: (key: string) => number[] }) => void;
type PolylineCall = {
  coordinates: [number, number][];
  options: { strokeColor?: string };
};

let clickHandlers: MapClickHandler[] = [];
let polylineCalls: PolylineCall[] = [];

const createFakeYmaps = () => {
  const map = {
    geoObjects: { add: vi.fn(), remove: vi.fn() },
    controls: { add: vi.fn() },
    events: {
      add: vi.fn((event: string, handler: MapClickHandler) => {
        if (event === 'click') clickHandlers.push(handler);
      }),
    },
    getZoom: vi.fn(() => 12),
    getBounds: vi.fn(() => [[59.8, 30.2], [60.0, 30.4]]),
    setBounds: vi.fn(),
    destroy: vi.fn(),
  };

  return {
    ready: (callback: () => void) => callback(),
    Map: vi.fn(() => map),
    GeoObjectCollection: vi.fn(() => ({ add: vi.fn(), removeAll: vi.fn() })),
    Placemark: vi.fn(() => ({
      events: { add: vi.fn() },
      geometry: { getCoordinates: () => [0, 0], setCoordinates: vi.fn() },
    })),
    Polyline: vi.fn(
      (
        coordinates: [number, number][],
        _properties: unknown,
        options: { strokeColor?: string }
      ) => {
        polylineCalls.push({ coordinates, options });
      }
    ),
    control: {
      ZoomControl: vi.fn(() => ({})),
      GeolocationControl: vi.fn(() => ({})),
    },
    util: { bounds: { fromPoints: vi.fn(() => ({})) } },
  };
};

const baseProps = {
  onPointChange: vi.fn(),
  routes: [] as Route[],
  selectedRouteIndex: 0,
  onSelectStop: vi.fn(),
  dummyMode: true,
  loadStops: vi.fn(async () => []),
};

const createMetroRoute = (line: number): Route => ({
  id: `route-metro-${line}`,
  name: `Метро ${line}`,
  distance_meters: 1000,
  geometry: {
    type: 'LineString',
    coordinates: [[30.31, 59.93], [30.32, 59.94]],
  },
  start_at: '2026-11-12T10:00:00+03:00',
  end_at: '2026-11-12T10:10:00+03:00',
  duration_seconds: 600,
  transfers: 0,
  legs: [
    {
      mode: 'metro',
      from: { lat: 59.93, lon: 30.31, name: 'A' },
      to: { lat: 59.94, lon: 30.32, name: 'B' },
      start_at: '2026-11-12T10:00:00+03:00',
      end_at: '2026-11-12T10:10:00+03:00',
      duration_seconds: 600,
      original_duration_seconds: 600,
      duration_source: 'schedule',
      line: { short_name: String(line), route_id: `metro:${line}` },
      geometry: {
        type: 'LineString',
        coordinates: [[30.31, 59.93], [30.32, 59.94]],
      },
      distance_meters: 1000,
    },
  ],
  provenance: { candidate_providers: [], traffic_providers: [] },
});

beforeEach(() => {
  clickHandlers = [];
  polylineCalls = [];
  (globalThis as unknown as { ymaps: unknown }).ymaps = createFakeYmaps();
  baseProps.onPointChange.mockClear();
});

afterEach(() => cleanup());

describe('YandexMap point selection', () => {
  it('selects point B on the second map click once point A is set', () => {
    const { rerender } = render(
      <YandexMap {...baseProps} pointA={null} pointB={null} />
    );

    expect(clickHandlers).toHaveLength(1);

    act(() => {
      clickHandlers[0]({ get: () => [59.93, 30.31] });
    });
    expect(baseProps.onPointChange).toHaveBeenLastCalledWith('A', {
      lat: 59.93,
      lon: 30.31,
    });

    const pointA: Point = { lat: 59.93, lon: 30.31 };
    rerender(<YandexMap {...baseProps} pointA={pointA} pointB={null} />);

    act(() => {
      clickHandlers[0]({ get: () => [59.95, 30.35] });
    });
    expect(baseProps.onPointChange).toHaveBeenLastCalledWith('B', {
      lat: 59.95,
      lon: 30.35,
    });
  });
});

describe('YandexMap metro line colors', () => {
  it.each([
    { line: 1, color: '#D6083B', name: 'red' },
    { line: 2, color: '#0078C9', name: 'blue' },
    { line: 3, color: '#009A49', name: 'green' },
    { line: 4, color: '#EA7125', name: 'orange' },
    { line: 5, color: '#702785', name: 'violet' },
    { line: 6, color: '#A86B32', name: 'brown' },
  ])('uses $name for metro line $line', ({ line, color }) => {
    render(
      <YandexMap
        {...baseProps}
        pointA={null}
        pointB={null}
        routes={[createMetroRoute(line)]}
      />
    );

    expect(polylineCalls.map((call) => call.options.strokeColor)).toContain(color);
  });
});
