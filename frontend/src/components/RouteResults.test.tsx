import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import { RouteResults } from './RouteResults';
import { Route } from '../types/api';

const createRoute = (durationSeconds: number): Route => ({
  id: 'route-1',
  name: 'Маршрут 1',
  distance_meters: 3200,
  geometry: {
    type: 'LineString',
    coordinates: [[30.31, 59.93], [30.32, 59.94]],
  },
  start_at: '2026-11-12T10:00:00+03:00',
  end_at: '2026-11-12T11:05:00+03:00',
  duration_seconds: durationSeconds,
  transfers: 0,
  legs: [
    {
      mode: 'bus',
      from: { lat: 59.93, lon: 30.31, name: 'A' },
      to: { lat: 59.94, lon: 30.32, name: 'B' },
      start_at: '2026-11-12T10:00:00+03:00',
      end_at: '2026-11-12T10:10:00+03:00',
      duration_seconds: 600,
      original_duration_seconds: 600,
      duration_source: 'schedule',
      line: { short_name: '22', route_id: 'bus:22' },
      geometry: {
        type: 'LineString',
        coordinates: [[30.31, 59.93], [30.32, 59.94]],
      },
      distance_meters: 3200,
    },
  ],
  provenance: { candidate_providers: [], traffic_providers: [] },
  fare: {
    currency: 'RUB',
    amount_rub: 176,
    items: [{ mode: 'bus', rides: 1, cost_rub: 176 }],
    transfers: 0,
    transfer_cost_rub: 0,
  },
});

afterEach(() => cleanup());

describe('RouteResults duration formatting', () => {
  it('shows minutes only for durations below one hour', () => {
    const { container } = render(
      <RouteResults
        routes={[createRoute(45 * 60)]}
        selectedIndex={-1}
        onSelectRoute={() => {}}
        isLoading={false}
      />
    );

    expect(container.textContent).toContain('45 мин');
    expect(container.textContent).not.toContain('0 ч');
  });

  it('shows hours and minutes for 65 minutes', () => {
    const { container } = render(
      <RouteResults
        routes={[createRoute(65 * 60)]}
        selectedIndex={-1}
        onSelectRoute={() => {}}
        isLoading={false}
      />
    );

    expect(container.textContent).toContain('1 ч 5 мин');
    expect(container.textContent).not.toContain('65 мин');
  });

  it('omits zero minutes when the duration is a whole number of hours', () => {
    const { container } = render(
      <RouteResults
        routes={[createRoute(2 * 60 * 60)]}
        selectedIndex={-1}
        onSelectRoute={() => {}}
        isLoading={false}
      />
    );

    expect(container.textContent).toContain('2 ч');
    expect(container.textContent).not.toContain('0 мин');
  });
});

describe('RouteResults wait legs', () => {
  it('shows only the wait label and time without from/to names', () => {
    const route = createRoute(10 * 60);
    route.legs = [
      {
        mode: 'wait',
        from: { lat: 59.93, lon: 30.31, name: 'Станция отправления' },
        to: { lat: 59.93, lon: 30.31, name: 'Станция прибытия' },
        start_at: '2026-11-12T10:00:00+03:00',
        end_at: '2026-11-12T10:05:00+03:00',
        duration_seconds: 300,
        original_duration_seconds: 300,
        duration_source: 'schedule',
        line: null,
        geometry: {
          type: 'LineString',
          coordinates: [[30.31, 59.93], [30.31, 59.93]],
        },
        distance_meters: 0,
      },
    ];

    const { container } = render(
      <RouteResults
        routes={[route]}
        selectedIndex={0}
        onSelectRoute={() => {}}
        isLoading={false}
      />
    );

    expect(container.textContent).toContain('Ожидание');
    expect(container.textContent).toMatch(/\d{2}:\d{2}/);
    expect(container.textContent).not.toContain('Станция отправления');
    expect(container.textContent).not.toContain('Станция прибытия');
  });
});

describe('RouteResults fare', () => {
  it('shows the route price in rubles', () => {
    const { container } = render(
      <RouteResults
        routes={[createRoute(10 * 60)]}
        selectedIndex={-1}
        onSelectRoute={() => {}}
        isLoading={false}
      />
    );

    expect(container.textContent).toContain('176 ₽');
  });

  it('keeps two decimals for fractional prices', () => {
    const route = createRoute(10 * 60);
    route.fare = { ...route.fare!, amount_rub: 123.45 };

    const { container } = render(
      <RouteResults
        routes={[route]}
        selectedIndex={-1}
        onSelectRoute={() => {}}
        isLoading={false}
      />
    );

    expect(container.textContent).toContain('123.45 ₽');
  });

  it('does not show a price when fare is missing', () => {
    const route = createRoute(10 * 60);
    route.fare = null;

    const { container } = render(
      <RouteResults
        routes={[route]}
        selectedIndex={-1}
        onSelectRoute={() => {}}
        isLoading={false}
      />
    );

    expect(container.textContent).not.toContain('₽');
  });
});

describe('RouteResults selected card', () => {
  it('uses opaque colors for the selected route card', () => {
    const { container } = render(
      <RouteResults
        routes={[createRoute(10 * 60)]}
        selectedIndex={0}
        onSelectRoute={() => {}}
        isLoading={false}
      />
    );

    const card = container.querySelector('.cursor-pointer');
    expect(card?.className).toContain('bg-blue-50');
    expect(card?.className).not.toContain('bg-blue-50/50');
    expect(card?.className).not.toContain('ring-blue-500/20');
  });
});
