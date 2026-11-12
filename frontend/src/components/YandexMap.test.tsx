import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';
import { YandexMap } from './YandexMap';
import { Point, Route } from '../types/api';

type MapClickHandler = (event: { get: (key: string) => number[] }) => void;

let clickHandlers: MapClickHandler[] = [];

const createFakeYmaps = () => {
  const map = {
    geoObjects: { add: vi.fn(), remove: vi.fn() },
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
    Polyline: vi.fn(),
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

describe('YandexMap point selection', () => {
  beforeEach(() => {
    clickHandlers = [];
    (globalThis as unknown as { ymaps: unknown }).ymaps = createFakeYmaps();
    baseProps.onPointChange.mockClear();
  });

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
