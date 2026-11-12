export type TransportMode = 'bus' | 'tram' | 'trolleybus' | 'metro' | 'rail';
export type RoutingMode = TransportMode | 'walk';
export type LegMode = RoutingMode | 'wait';

export type TicketProfile = 
  | 'podorozhnik_wallet'
  | 'unlimited_surface'
  | 'unlimited_metro'
  | 'unlimited_all'
  | 'bank_card';

export interface Point {
  lat: number;
  lon: number;
}

export interface GeocodedPlace extends Point {
  label: string;
  precision?: string | null;
}

export interface Suggestion {
  title: string;
  subtitle?: string | null;
  uri: string;
}

export interface SuggestResponse {
  mode: 'dummy' | 'yandex' | '2gis';
  provider: 'yandex' | '2gis';
  items: Suggestion[];
  warnings?: string[];
}

export interface GeocodeResponse {
  mode: 'dummy' | 'yandex' | '2gis';
  provider: 'yandex' | '2gis';
  items: GeocodedPlace[];
  warnings?: string[];
}

export interface LineStringGeometry {
  type: 'LineString';
  coordinates: [number, number][]; // [lon, lat] в формате GeoJSON
}

export interface TransitLine {
  short_name: string;
  route_id: string;
  trip_id?: string | null;
}

export interface Leg {
  mode: LegMode;
  from: { lat: number; lon: number; name: string; stop_id?: string | null };
  to: { lat: number; lon: number; name: string; stop_id?: string | null };
  start_at: string;
  end_at: string;
  duration_seconds: number;
  original_duration_seconds: number;
  duration_source: 'dummy' | 'schedule' | 'yandex' | '2gis' | 'estimated';
  line?: TransitLine | null;
  geometry: LineStringGeometry;
  distance_meters: number;
}

export interface FareItem {
  mode: TransportMode;
  rides: number;
  cost_rub: number;
}

export interface RouteFare {
  currency: 'RUB';
  amount_rub: number;
  items: FareItem[];
  transfers: number;
  transfer_cost_rub: number;
}

export interface Route {
  id: string;
  name: string;
  distance_meters: number;
  geometry: LineStringGeometry;
  start_at: string;
  end_at: string;
  duration_seconds: number;
  transfers: number;
  legs: Leg[];
  provenance: {
    candidate_providers: string[];
    traffic_providers: string[];
  };
  fare?: RouteFare | null;
}

export interface RouteRequest {
  points: [Point, Point];
  departure_at: string;
  traffic_mode?: 'off' | 'on';
  max_results?: number;
  providers?: ('otp' | 'yandex' | '2gis')[];
  modes?: RoutingMode[] | null;
  exclude_modes?: TransportMode[] | null;
  max_transfers?: number | null;
  walk_speed?: 'slow' | 'normal' | 'fast';
  less_walking?: boolean;
  passenger?: {
    ticket_profile: TicketProfile;
    uncovered_payment?: 'podorozhnik_wallet' | 'bank_card';
  };
}

export interface RoutesResponse {
  mode: 'dummy' | 'real';
  routes: Route[];
  warnings?: string[];
  calculated_at: string;
}

export interface GtfsStop {
  lat: number;
  lon: number;
  stop_id: string;
  name: string;
  code?: string | null;
  description?: string | null;
}

export interface GtfsStopsResponse {
  items: GtfsStop[];
  total: number;
  has_more: boolean;
}

export interface GtfsDeparture {
  trip_id: string;
  service_id: string;
  stop_id: string;
  line: {
    route_id: string;
    short_name: string;
    mode: TransportMode;
    color?: string | null;
  };
  headsign?: string | null;
  scheduled_departure_at?: string | null;
  departure_time?: string | null;
  schedule_type: 'timetable' | 'frequency';
  headway_seconds?: number | null;
}

export interface GtfsDeparturesResponse {
  stop_id: string;
  items: GtfsDeparture[];
}

export interface TicketProfileInfo {
  id: TicketProfile;
  label_ru: string;
  description_ru: string;
}
export interface DummyConfig {
  suggest: boolean;
  geocode: boolean;
  routes: boolean;
  stops: boolean;
  departures: boolean;
}
