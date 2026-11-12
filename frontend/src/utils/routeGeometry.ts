export type LonLat = [number, number];

const METERS_PER_DEGREE = 111320;
const METERS_PER_PIXEL_AT_ZOOM_0 = 156543.03392;
const ROUTE_SPACING_PIXELS = 6;

/**
 * Смещение маршрута в метрах. Маршруты раскладываются симметрично
 * относительно реальной геометрии, чтобы совпадающие участки были видны.
 */
export const getRouteOffsetMeters = (
  index: number,
  total: number,
  zoom: number,
  latitude: number
): number => {
  if (total < 2) return 0;

  const metersPerPixel =
    (METERS_PER_PIXEL_AT_ZOOM_0 * Math.cos((latitude * Math.PI) / 180)) /
    Math.pow(2, zoom);

  return (index - (total - 1) / 2) * ROUTE_SPACING_PIXELS * metersPerPixel;
};

/**
 * Сдвигает линию перпендикулярно её направлению в каждой точке.
 * Это визуальное смещение: реальные координаты маршрута не меняются.
 */
export const offsetPolyline = (
  coordinates: LonLat[],
  offsetMeters: number
): LonLat[] => {
  if (!coordinates.length || offsetMeters === 0) return coordinates;

  return coordinates.map(([lon, lat], index) => {
    const prev = coordinates[Math.max(0, index - 1)];
    const next = coordinates[Math.min(coordinates.length - 1, index + 1)];

    const metersPerLon = METERS_PER_DEGREE * Math.cos((lat * Math.PI) / 180);
    const dx = (next[0] - prev[0]) * metersPerLon;
    const dy = (next[1] - prev[1]) * METERS_PER_DEGREE;
    const length = Math.hypot(dx, dy);

    if (length === 0) return [lon, lat];

    const shiftX = (-dy / length) * offsetMeters;
    const shiftY = (dx / length) * offsetMeters;

    return [lon + shiftX / metersPerLon, lat + shiftY / METERS_PER_DEGREE] as LonLat;
  });
};
