import { describe, expect, it } from 'vitest';
import { Route, VehiclePosition } from '../types/api';
import { getVehiclesBBox, normalizeRouteId, vehicleMatchesRoute } from './vehicleFilters';

const vehicle = (patch: Partial<VehiclePosition>): VehiclePosition => ({
  vehicle_id: 'v1',
  route_id: 'metro:2',
  trip_id: 'trip-1',
  lat: 59.93,
  lon: 30.31,
  mode: 'metro',
  timestamp: '2026-11-13T10:00:00+03:00',
  ...patch,
});

const route = (routeId: string, tripId?: string): Pick<Route, 'legs'> => ({
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
      line: { short_name: '2', route_id: routeId, trip_id: tripId },
      geometry: { type: 'LineString', coordinates: [[30.31, 59.93], [30.32, 59.94]] },
      distance_meters: 1000,
    },
  ],
});

describe('normalizeRouteId', () => {
  it('strips the OTP feed namespace', () => {
    expect(normalizeRouteId('1:metro:2')).toBe('metro:2');
    expect(normalizeRouteId('metro:2')).toBe('metro:2');
  });
});

describe('vehicleMatchesRoute', () => {
  it('matches an OTP-prefixed route id to a GTFS vehicle route id', () => {
    expect(vehicleMatchesRoute(vehicle({ route_id: 'metro:2' }), route('1:metro:2'))).toBe(true);
  });

  it('matches by trip id', () => {
    expect(
      vehicleMatchesRoute(
        vehicle({ route_id: 'other', trip_id: 'trip-1' }),
        route('1:metro:2', 'trip-1')
      )
    ).toBe(true);
  });

  it('rejects vehicles from another route', () => {
    expect(vehicleMatchesRoute(vehicle({ route_id: 'metro:3' }), route('1:metro:2'))).toBe(false);
  });
});

describe('getVehiclesBBox', () => {
  it('returns a padded minLon,minLat,maxLon,maxLat box', () => {
    const bbox = getVehiclesBBox([
      { geometry: { type: 'LineString', coordinates: [[30.31, 59.93], [30.32, 59.94]] } },
    ]);

    expect(bbox).toBe('30.290000,59.910000,30.340000,59.960000');
  });

  it('returns null without route geometry', () => {
    expect(getVehiclesBBox([])).toBeNull();
  });
});
