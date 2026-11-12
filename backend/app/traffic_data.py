"""Internal immutable traffic observations; coordinates are longitude, latitude."""
from dataclasses import dataclass, field
from datetime import datetime
import math

TTL_SECONDS = 1800

@dataclass(frozen=True)
class RoadObservation:
    id: str
    provider: str
    geometry: tuple
    length_m: float
    duration_seconds: float
    received_at: float
    expires_at: float | None = None
    alternative_index: int = 0
    part_index: int = 0

    def __post_init__(self):
        object.__setattr__(self, 'geometry', tuple(tuple(p) for p in self.geometry))
        if len(self.geometry) < 2 or any(len(p)!=2 or not all(math.isfinite(v) for v in p) or not (-180<=p[0]<=180 and -90<=p[1]<=90) for p in self.geometry):
            raise ValueError('Invalid observation geometry')
        if any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in (self.length_m, self.duration_seconds, self.received_at)):
            raise ValueError('Invalid observation measurement')
        if self.expires_at is None:
            object.__setattr__(self, 'expires_at', self.received_at + TTL_SECONDS)
        elif not math.isfinite(self.expires_at):
            raise ValueError('Invalid expiry')
        else:
            object.__setattr__(self, 'expires_at', min(self.expires_at, self.received_at + TTL_SECONDS))

    def fresh(self, now):
        return self.received_at <= now < self.expires_at

@dataclass(frozen=True)
class SegmentBaseline:
    feed_id: str
    gtfs_version: str | None
    trip_id: str
    from_stop_id: str
    to_stop_id: str
    from_sequence: int
    to_sequence: int
    geometry: tuple
    moving_seconds: float | None
    dwell_seconds: float = 0
    service_id: str | None = None
    ambiguous: bool = False
    service_day: str | None = None
    mode: str = 'bus'
    road_status: str = 'unknown'
    lane_source_url: str | None = None
    lane_source_observed_at: datetime | None = None
    road_status_version: str | None = None
    lane_spans: tuple = ()

    @property
    def edge_key(self):
        return self.feed_id, self.from_stop_id, self.to_stop_id

    @property
    def occurrence_id(self):
        return (self.feed_id, self.trip_id, self.service_day, self.from_sequence, self.to_sequence)

@dataclass
class CorrectionResult:
    added_seconds: float = 0
    covered_m: float = 0
    applicable_m: float = 0
    fragments: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
