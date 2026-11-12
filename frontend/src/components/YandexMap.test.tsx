import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { YandexMap } from './YandexMap';
import { LegMode, Point, Route } from '../types/api';

type MapClickHandler = (event: { get: (key: string) => number[] }) => void;
type PolylineClickHandler = (event: { stopPropagation: () => void }) => void;
type PolylineCall = {
  coordinates: [number, number][];
  options: { strokeColor?: string; strokeOpacity?: number };
};

let clickHandlers: MapClickHandler[] = [];
let polylineCalls: PolylineCall[] = [];
let polylineClickHandlers: { color?: string; handler: PolylineClickHandler }[] = [];

const createFakeYmaps = ({ zoom = 12 }: { zoom?: number } = {}) => {
  const map = {
    geoObjects: { add: vi.fn(), remove: vi.fn() },
    controls: { add: vi.fn() },
    events: {
      add: vi.fn((event: string, handler: MapClickHandler) => {
        if (event === 'click') clickHandlers.push(handler);
      }),
    },
    getZoom: vi.fn(() => zoom),
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
        options: PolylineCall['options']
      ) => {
        polylineCalls.push({ coordinates, options });
        return {
          events: {
            add: vi.fn((event: string, handler: PolylineClickHandler) => {
              if (event === 'click') {
                polylineClickHandlers.push({ color: options.strokeColor, handler });
              }
            }),
          },
        };
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
  onSelectRoute: vi.fn(),
  onSelectStop: vi.fn(),
  dummyMode: true,
  dummyStops: true,
  loadStops: vi.fn(async (_bbox: string) => []),
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
  polylineClickHandlers = [];
  (globalThis as unknown as { ymaps: unknown }).ymaps = createFakeYmaps();
  baseProps.onPointChange.mockClear();
  baseProps.onSelectRoute.mockClear();
  baseProps.loadStops.mockClear();
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

describe('YandexMap stops', () => {
  it('loads stops immediately after map init when the zoom is high enough', async () => {
    (globalThis as unknown as { ymaps: unknown }).ymaps = createFakeYmaps({ zoom: 16 });
    const loadStops = vi.fn(async (_bbox: string) => [
      { lat: 59.94, lon: 30.33, stop_id: 's1', name: 'Остановка' },
    ]);

    render(
      <YandexMap
        {...baseProps}
        pointA={null}
        pointB={null}
        loadStops={loadStops}
      />
    );

    await waitFor(() => expect(loadStops).toHaveBeenCalledTimes(1));
    expect(loadStops.mock.calls[0][0]).toMatch(
      /^-?\d+\.\d+,-?\d+\.\d+,-?\d+\.\d+,-?\d+\.\d+$/
    );
  });
});

describe('YandexMap transport colors', () => {
  const cases: { mode: LegMode; color: string }[] = [
    { mode: 'walk', color: '#7C3AED' },
    { mode: 'bus', color: '#65A30D' },
    { mode: 'trolleybus', color: '#06B6D4' },
    { mode: 'tram', color: '#DB2777' },
    { mode: 'rail', color: '#374151' },
  ];

  it.each(cases)('uses $color for $mode', ({ mode, color }) => {
    const route = createMetroRoute(1);
    route.legs[0] = { ...route.legs[0], mode, line: null };

    render(
      <YandexMap {...baseProps} pointA={null} pointB={null} routes={[route]} />
    );

    expect(polylineCalls.map((call) => call.options.strokeColor)).toContain(color);
  });
});

describe('YandexMap route selection', () => {
  it('selects an unselected route when its line is clicked', () => {
    render(
      <YandexMap
        {...baseProps}
        pointA={null}
        pointB={null}
        routes={[createMetroRoute(1), createMetroRoute(2)]}
        selectedRouteIndex={0}
      />
    );

    const greyLine = polylineClickHandlers.find((item) => item.color === '#94A3B8');
    expect(greyLine).toBeDefined();

    const stopPropagation = vi.fn();
    act(() => {
      greyLine!.handler({ stopPropagation });
    });

    expect(stopPropagation).toHaveBeenCalledTimes(1);
    expect(baseProps.onSelectRoute).toHaveBeenCalledWith(1);
  });
});

describe('YandexMap overlapping routes', () => {
  it('keeps coincident routes in place and draws the selected route on top', () => {
    const first = createMetroRoute(1);
    const second = { ...createMetroRoute(1), id: 'route-2' };

    render(
      <YandexMap
        {...baseProps}
        pointA={null}
        pointB={null}
        routes={[first, second]}
        selectedRouteIndex={0}
      />
    );

    const latest = polylineCalls.slice(-2);
    expect(latest).toHaveLength(2);

    // Невыбранный маршрут: серый и слегка прозрачный
    expect(latest[0].options.strokeColor).toBe('#94A3B8');
    expect(latest[0].options.strokeOpacity).toBe(0.55);

    // Выбранный маршрут: цветной, непрозрачный и нарисован последним
    expect(latest[1].options.strokeColor).toBe('#D6083B');
    expect(latest[1].options.strokeOpacity).toBe(1);

    // Геометрия не смещается
    expect(latest[0].coordinates).toEqual(latest[1].coordinates);
  });
});

describe('YandexMap metro line colors', () => {
  it('keeps the selected route opaque and makes other routes slightly transparent', () => {
    render(
      <YandexMap
        {...baseProps}
        pointA={null}
        pointB={null}
        routes={[createMetroRoute(1), createMetroRoute(2)]}
        selectedRouteIndex={1}
      />
    );

    expect(polylineCalls.length).toBeGreaterThan(0);
    expect(
      polylineCalls.some(
        (call) => call.options.strokeColor === '#0078C9' && call.options.strokeOpacity === 1
      )
    ).toBe(true);
    expect(
      polylineCalls.some(
        (call) => call.options.strokeColor === '#94A3B8' && call.options.strokeOpacity === 0.55
      )
    ).toBe(true);
  });

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

  it('ignores the OTP feed namespace prefix in gtfsId', () => {
    const route = createMetroRoute(2);
    route.legs[0] = {
      ...route.legs[0],
      line: { short_name: '2', route_id: '1:metro:2' },
    };

    render(
      <YandexMap {...baseProps} pointA={null} pointB={null} routes={[route]} />
    );

    expect(polylineCalls.map((call) => call.options.strokeColor)).toContain('#0078C9');
    expect(polylineCalls.map((call) => call.options.strokeColor)).not.toContain('#D6083B');
  });
});
