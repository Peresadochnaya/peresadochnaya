import { Route, VehiclePosition } from '../types/api';

// OTP prefixes GTFS ids with the feed namespace, e.g. "1:metro:2".
export const normalizeRouteId = (routeId?: string | null) =>
  (routeId ?? '').replace(/^\d+:/, '').toLowerCase();

export const vehicleMatchesRoute = (
  vehicle: Pick<VehiclePosition, 'route_id' | 'trip_id'>,
  route: Pick<Route, 'legs'>
) =>
  route.legs.some((leg) => {
    if (!leg.line) return false;
    if (leg.line.trip_id && vehicle.trip_id && leg.line.trip_id === vehicle.trip_id) {
      return true;
    }
    return normalizeRouteId(leg.line.route_id) === normalizeRouteId(vehicle.route_id);
  });

const BBOX_PADDING = 0.02;
const MIN_LON = -180;
const MAX_LON = 180;
const MIN_LAT = -90;
const MAX_LAT = 90;

export const getVehiclesBBox = (routes: Pick<Route, 'geometry'>[]): string | null => {
  const coordinates = routes.flatMap((route) => route.geometry.coordinates);
  if (!coordinates.length) return null;

  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;

  for (const [lon, lat] of coordinates) {
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
  }

  minLon = Math.max(MIN_LON, minLon - BBOX_PADDING);
  maxLon = Math.min(MAX_LON, maxLon + BBOX_PADDING);
  minLat = Math.max(MIN_LAT, minLat - BBOX_PADDING);
  maxLat = Math.min(MAX_LAT, maxLat + BBOX_PADDING);

  if (!(minLon < maxLon && minLat < maxLat)) return null;

  return `${minLon.toFixed(6)},${minLat.toFixed(6)},${maxLon.toFixed(6)},${maxLat.toFixed(6)}`;
};
