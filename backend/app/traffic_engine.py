"""Bounded road-observation orchestration and OTP itinerary correction.

Road snapshots never replace the scheduled path. Missing observations remain
explicitly incomplete; recovery replaces only the tail after alighting.
"""
import inspect
import json
import math
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import schemas as s
from .otp import SURFACE_MODES, _line, _route_name, _wait_leg, route_fare, TRAFFIC_WARNING
from .route_selection import is_walking_route
from .traffic_runtime import run_bounded
from .traffic_data import CorrectionResult
from .yandex import ProviderError
from .traffic_timing import classify_coverage, transfer_decision

ALGORITHM_VERSION = 'road-correction-1'
SEARCH_SECONDS = 15.0


def _invoke(function, *args, **kwargs):
    """Small test/provider adapters may omit optional budget keywords."""
    parameters = inspect.signature(function).parameters
    if not any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in parameters}
    return function(*args, **kwargs)


def final_journey_key(route):
    """Exact final passenger journey; never a route/minute similarity rule."""
    return json.dumps([leg.model_dump(mode='json',by_alias=True,
        exclude={'debug','original_duration_seconds','duration_source'}) for leg in route.legs],
        sort_keys=True,separators=(',',':'))


def collapse_replanned_duplicates(routes, originals):
    groups={}
    for route in routes:
        groups.setdefault(final_journey_key(route),[]).append(route)
    retained=[]; excluded=[]
    for group in groups.values():
        if len(group)<2 or not any(r.debug.tail_replanning_status=='complete' for r in group):
            retained.extend(group); continue
        winner=min(group,key=lambda r:(r.debug.evaluation_group=='incomplete',
            -(r.debug.coverage.usable_coverage_ratio or 0),r.debug.original_rank,r.id))
        retained.append(winner)
        for route in group:
            if route is winner: continue
            winner.provenance.candidate_providers=sorted(set(winner.provenance.candidate_providers+route.provenance.candidate_providers))
            winner.provenance.traffic_providers=sorted(set(winner.provenance.traffic_providers+route.provenance.traffic_providers))
            winner.debug.equivalent_candidate_ids.append(route.debug.original_otp_route_id)
            excluded.append(s.ExcludedCandidateDebug(reason_code='duplicate_after_replanning',
                reason='Distinct OTP candidates converged to exactly identical final legs after tail replanning',
                original_route=originals[route.debug.original_rank-1],original_rank=route.debug.original_rank,
                duplicate_of_route_id=winner.id))
    return retained,excluded


def coverage(eligible, covered):
    covered = min(eligible, max(0., covered))
    return s.TrafficCoverage(eligible_distance_meters=eligible,
        matched_distance_meters=covered, usable_distance_meters=covered,
        uncovered_distance_meters=max(0., eligible-covered),
        coverage_ratio=covered/eligible if eligible else None,
        usable_coverage_ratio=covered/eligible if eligible else None)


def recompute_route(route, request):
    route.start_at = route.legs[0].start_at
    route.end_at = route.legs[-1].end_at
    route.duration_seconds = max(0, int((route.end_at-route.start_at).total_seconds()))
    route.distance_meters = sum(l.distance_meters for l in route.legs)
    route.transfers = max(0, sum(l.line is not None for l in route.legs)-1)
    route.geometry = _line([p for l in route.legs for p in l.geometry.coordinates])
    route.name = _route_name(route.legs)
    route.fare = route_fare(route.legs, request.fare, request.passenger)
    return route


class TrafficEngine:
    def __init__(self, planner, roads=None, dgis_roads=None, graph_loader=None, *,
                 clock=time.monotonic, wall_clock=time.time, collector=None, transfer_slack_loader=None):
        self.planner, self.roads, self.dgis_roads = planner, roads, dgis_roads
        self.graph_loader, self.collector = graph_loader, collector
        self.transfer_slack_loader = transfer_slack_loader
        self._search_slots=threading.BoundedSemaphore(2)
        self.clock, self.wall_clock = clock, wall_clock

    def plan(self, request, *, gtfs_version=None, graph_version=None):
        if not self._search_slots.acquire(blocking=False):
            raise ProviderError(503,'traffic_search_busy','Traffic search capacity is busy; try again later')
        try:
            return self._plan(request,gtfs_version=gtfs_version,graph_version=graph_version)
        finally:
            self._search_slots.release()

    def _plan(self, request, *, gtfs_version=None, graph_version=None):
        deadline = self.clock() + SEARCH_SECONDS
        calls = []
        def otp(req, purpose='initial_candidates', origin_stop_id=None, optional=False):
            remaining = deadline-self.clock()-.05
            if remaining <= 0:
                raise TimeoutError('search_deadline')
            if optional:
                remaining = min(1.0, remaining)
            started = self.clock()
            try:
                result = run_bounded(lambda: _invoke(self.planner.plan, req, gtfs_version=gtfs_version,
                    graph_version=graph_version, timeout_seconds=remaining, origin_stop_id=origin_stop_id,
                    exclude_realtime_updates=True, ensure_walking=purpose == 'initial_candidates' and not optional),
                    min(deadline-.05,self.clock()+remaining),self.clock)
            except Exception as exc:
                calls.append(s.ProviderCallDebug(provider='otp', purpose=purpose, status='failed',
                    error_code=getattr(exc, 'code', type(exc).__name__), billable_units=0,
                    elapsed_milliseconds=max(0,(self.clock()-started)*1000)))
                raise
            calls.append(s.ProviderCallDebug(provider='otp', purpose=purpose, status='succeeded',
                billable_units=0, elapsed_milliseconds=max(0,(self.clock()-started)*1000)))
            return result
        try:
            response = otp(request)
        except TimeoutError:
            raise ProviderError(504,'planner_timeout','Route planner exceeded the search deadline') from None
        candidates = list(response.routes)
        enabled = set(request.modes or ['bus','tram','trolleybus','metro','rail'])-set(request.exclude_modes or [])
        enabled.discard('walk')
        if not enabled:
            # Walking has no road-traffic correction; do not load or collect
            # car observations (including billable providers) for this request.
            for rank, route in enumerate(response.routes, 1):
                for leg in route.legs:
                    leg.debug = s.LegDebug(traffic_status='not_applicable',
                        original_start_at=leg.start_at, original_end_at=leg.end_at)
                route.debug = s.RouteDebug(evaluation_group='not_applicable',
                    tail_replanning_status='not_needed', original_otp_route_id=route.id,
                    original_rank=rank, final_rank=rank,
                    original_duration_seconds=route.duration_seconds,
                    corrected_duration_seconds=route.duration_seconds, added_delay_seconds=0,
                    coverage=coverage(0, 0), transfer_status='not_applicable',
                    ranking_reason='walking route; road correction not applicable')
            response.debug = s.RoutesDebug(pipeline_status='complete', trace_id=uuid.uuid4().hex,
                algorithm_version=ALGORITHM_VERSION, provider_calls=calls,
                candidate_count=len(candidates), returned_count=len(response.routes), excluded_count=0,
                time_budget_seconds=SEARCH_SECONDS, deadline_reached=self.clock()>=deadline-.05,
                otp_replanning_count=0, baseline_gtfs_version=gtfs_version,
                graph_version=graph_version, coverage=coverage(0, 0))
            return response
        # OTP may omit a useful mode before postfiltering. Keep every user's
        # preference while requesting that mode independently.
        for mode in sorted(enabled):
            if len(enabled) <= 1 or deadline-self.clock() < 6:
                break
            try:
                extra = otp(request.model_copy(update={'modes':[mode]}),optional=True)
                seen = {r.id for r in candidates}
                candidates.extend(r for r in extra.routes if r.id not in seen)
            except Exception:
                continue
        graph = None
        graph_warning = None
        try:
            if self.graph_loader is not None:
                graph = run_bounded(lambda:_invoke(self.graph_loader, gtfs_version=gtfs_version, graph_version=graph_version),deadline-.05,self.clock)
        except Exception as exc:
            graph_warning = 'baseline_unavailable:' + type(exc).__name__
        observations = []
        if self.collector is not None and self.clock() < deadline:
            try:
                observations, collected = _invoke(self.collector, [r for r in candidates if not is_walking_route(r)], graph, deadline, request=request)
                calls.extend(collected)
            except Exception as exc:
                graph_warning = 'collection_unavailable:' + type(exc).__name__
        originals = [r.model_copy(deep=True) for r in candidates]
        corrected, excluded = [], []
        replan_count = 0
        for rank, (candidate, original) in enumerate(zip(candidates, originals),1):
            route, used_replans, reason = self._correct(candidate.model_copy(deep=True), original,
                request, graph, observations, deadline, otp, rank)
            replan_count += used_replans
            if reason:
                excluded.append(s.ExcludedCandidateDebug(reason_code='boarding_unreachable',
                    reason=reason, original_route=original, original_rank=rank, tail_replanning_status='failed'))
            else:
                corrected.append(route)
        if not corrected and candidates and self.clock() < deadline-.05:
            # A valid scheduled search can lose every candidate to unreachable
            # boardings. Ask the planner for a real walking path within the
            # same deadline rather than fabricating a replacement itinerary.
            try:
                fallback = otp(request.model_copy(update={'modes':['walk'], 'exclude_modes':[]}),
                    purpose='initial_candidates')
                for candidate in fallback.routes:
                    if not is_walking_route(candidate):
                        continue
                    original = candidate.model_copy(deep=True)
                    candidates.append(candidate)
                    originals.append(original)
                    route, _, _ = self._correct(candidate.model_copy(deep=True), original,
                        request, graph, [], deadline, otp, len(candidates))
                    corrected.append(route)
            except Exception:
                pass
        corrected, duplicate_exclusions=collapse_replanned_duplicates(corrected,originals)
        excluded.extend(duplicate_exclusions)
        corrected.sort(key=lambda r: (r.debug.evaluation_group == 'incomplete',
            r.end_at, r.transfers, sum(l.distance_meters for l in r.legs if l.mode=='walk'), r.id))
        response.routes = corrected[:request.max_results]
        for rank, route in enumerate(response.routes,1):
            route.debug.final_rank = rank
        response.warnings = [w for w in response.warnings if w != TRAFFIC_WARNING]
        response.warnings.append('Current car observations are an estimate for public transport, not predicted vehicle arrivals')
        if graph_warning:
            response.warnings.append(graph_warning)
        response.sources.traffic_providers = sorted({p for r in response.routes for p in r.provenance.traffic_providers})
        response.debug = s.RoutesDebug(pipeline_status='partial' if any(r.debug.evaluation_group=='incomplete' for r in corrected) or any(e.reason_code!='duplicate_after_replanning' for e in excluded) else 'complete',
            trace_id=uuid.uuid4().hex, algorithm_version=ALGORITHM_VERSION, cache_ttl_seconds=1800,
            observations=self._observation_debug(CorrectionResult(fragments=[f for r in corrected for l in r.legs if l.debug for seg in l.debug.segments for f in seg.fragments]),observations,response.calculated_at,include_all=True),
            observation_geometries={o.id:s.LineString(coordinates=list(o.geometry)) for o in observations},
            provider_calls=calls, candidate_count=len(candidates), returned_count=len(response.routes),
            excluded_count=len(excluded), excluded_candidates=excluded, time_budget_seconds=SEARCH_SECONDS,
            deadline_reached=self.clock()>=deadline-.05, otp_replanning_count=replan_count,
            baseline_gtfs_version=gtfs_version, graph_version=graph_version,
            coverage=coverage(sum(r.debug.coverage.eligible_distance_meters for r in corrected),
                              sum(r.debug.coverage.usable_distance_meters for r in corrected)),
            warnings=[graph_warning] if graph_warning else [])
        return response

    def _service(self, graph, leg, deadline):
        info = None
        if graph is not None and leg.line and hasattr(graph,'service_info'):
            try:
                info = run_bounded(lambda:_invoke(graph.service_info,leg.line.trip_id,service_day=None,
                    departure_at=leg.start_at,boarding_stop_id=leg.origin.stop_id,
                    feed_id=leg.mode if leg.mode in ('metro','rail') else 'gtfs'),deadline-.05,self.clock)
            except Exception:
                info = None
        if info:
            return s.TransitServiceDebug(**{k:v for k,v in info.items() if k in s.TransitServiceDebug.model_fields})
        return s.TransitServiceDebug(service_kind='unknown', wait_model='unknown')

    def _correct(self, route, original, request, graph, observations, deadline, otp, rank):
        if is_walking_route(route):
            for leg in route.legs:
                leg.debug = s.LegDebug(traffic_status='not_applicable',
                    original_start_at=leg.start_at, original_end_at=leg.end_at)
            route.debug = s.RouteDebug(evaluation_group='not_applicable',
                tail_replanning_status='not_needed', original_otp_route_id=original.id,
                original_rank=rank, original_duration_seconds=original.duration_seconds,
                corrected_duration_seconds=route.duration_seconds, added_delay_seconds=0,
                coverage=coverage(0, 0), transfer_status='not_applicable',
                ranking_reason='walking route; road correction not applicable')
            return route, 0, None
        from .traffic_geometry import correct_segment
        result_legs, events, seen = [], [], set()
        handled = set()
        applicable = covered = 0.
        try:
            slack = run_bounded(self.transfer_slack_loader,deadline-.05,self.clock) if self.transfer_slack_loader else 0.
        except Exception:
            slack = None
        slack_unknown = slack is None
        slack = slack or 0.
        unverified = False
        replans = 0
        tail_status = 'not_needed'
        pending = list(route.legs)
        providers = set()
        while pending:
            leg = pending.pop(0).model_copy(deep=True)
            if leg.mode == 'wait':
                if not result_legs:
                    result_legs.append(leg)
                # OTP wait is rebuilt at the next boarding, so delayed arrivals
                # consume waiting rather than moving a scheduled departure.
                continue
            original_start, original_end = leg.start_at, leg.end_at
            if leg.mode == 'walk' and result_legs:
                leg.start_at = result_legs[-1].end_at
                leg.end_at = leg.start_at+timedelta(seconds=leg.duration_seconds)
            if leg.line:
                service = self._service(graph,leg,deadline)
                previous = next((x for x in reversed(result_legs) if x.line),None)
                if result_legs:
                    arrival = result_legs[-1].end_at
                    walking = sum(x.duration_seconds for x in result_legs[result_legs.index(previous)+1:] if x.mode=='walk') if previous else 0
                    transfer_minimum = None
                    forbidden = False
                    if previous and graph is not None and hasattr(graph,'transfer_minimum'):
                        try:
                            info = run_bounded(lambda:_invoke(graph.transfer_minimum,previous.destination.stop_id,leg.origin.stop_id,
                                from_trip_id=previous.line.trip_id,to_trip_id=leg.line.trip_id),deadline-.05,self.clock)
                        except Exception:
                            info = {}
                        transfer_minimum=info.get('minimum_seconds'); forbidden=info.get('forbidden',False)
                    minimum = max(walking+slack,transfer_minimum or 0)
                    decision = transfer_decision(arrival,leg.start_at,max(0,minimum-walking),
                        service.frequency_exact_times==0, previous is not None and previous.end_at > (previous.debug.original_end_at or previous.end_at))
                    if forbidden:
                        from .traffic_timing import TransferDecision
                        decision=TransferDecision(False,True,-1)
                    boundary = (previous.destination.stop_id,previous.end_at.isoformat(),leg.line.trip_id) if previous else None
                    if previous and decision.replan and boundary not in handled:
                        tail_status = 'pending'
                        walking = sum(x.duration_seconds for x in result_legs[result_legs.index(previous)+1:] if x.mode=='walk')
                        boundary = (previous.destination.stop_id,previous.end_at.isoformat(),leg.line.trip_id)
                        event = s.TransferDebug(from_leg_index=result_legs.index(previous),to_leg_index=len(result_legs),
                            stop_id=previous.destination.stop_id,status='feasible' if decision.reachable else 'missed',
                            next_service=service,corrected_arrival_at=previous.end_at,
                            modeled_boarding_at=leg.start_at if service.frequency_exact_times==0 else None,
                            scheduled_departure_at=leg.start_at if service.frequency_exact_times!=0 else None,
                            modeled_boarding_reachable=decision.reachable,tail_replanning_required=True,
                            minimum_transfer_seconds=minimum,remaining_slack_seconds=decision.slack_seconds,
                            resolution='unresolved')
                        events.append(event)
                        recovered = False
                        if boundary not in seen and self.clock()<deadline-.05:
                            seen.add(boundary)
                            prefix = result_legs[:result_legs.index(previous)+1]
                            boardings = sum(x.line is not None for x in prefix)
                            remaining = None if request.max_transfers is None else request.max_transfers-boardings
                            if remaining is None or remaining>=0:
                                tail_request = request.model_copy(update={'points':[s.Point(lat=previous.destination.lat,lon=previous.destination.lon),request.points[1]],
                                    'departure_at':previous.end_at,'max_transfers':remaining})
                                try:
                                    replans += 1
                                    tail = otp(tail_request,'transfer_replanning',previous.destination.stop_id)
                                    valid = [r for r in tail.routes if r.legs and r.start_at>=previous.end_at and self._valid_tail(r,previous,slack,graph,deadline) and
                                        (request.max_transfers is None or boardings+sum(x.line is not None for x in r.legs)-1<=request.max_transfers)]
                                    if valid:
                                        chosen = min(valid,key=lambda r:r.end_at)
                                        new_first = next((x for x in chosen.legs if x.line),None)
                                        signature = (previous.destination.stop_id,previous.end_at.isoformat(), new_first.line.trip_id if new_first else None)
                                        if signature not in handled:
                                            result_legs = prefix
                                            pending = [x.model_copy(deep=True) for x in chosen.legs]
                                            # Mark the recovered boarding boundary as handled;
                                            # following delayed rides can trigger further recovery.
                                            if new_first:
                                                handled.add(signature)
                                            event.resolution='otp_replanned'; tail_status='complete'; recovered=True
                                except Exception:
                                    pass
                        if recovered:
                            continue
                        if not decision.reachable:
                            return route,replans,'Unreachable boarding; replacement tail unavailable or made no progress'
                        unverified=True; tail_status='unverified_fallback'; event.resolution='kept_unverified'
                    if previous and not decision.replan:
                        events.append(s.TransferDebug(from_leg_index=result_legs.index(previous),to_leg_index=len(result_legs),
                            stop_id=previous.destination.stop_id,status='unknown' if slack_unknown else 'feasible',next_service=service,
                            corrected_arrival_at=previous.end_at,
                            scheduled_departure_at=leg.start_at if service.service_kind=='scheduled' or service.frequency_exact_times==1 else None,
                            modeled_boarding_at=leg.start_at if service.frequency_exact_times==0 else None,
                            modeled_boarding_reachable=decision.reachable,tail_replanning_required=False,
                            minimum_transfer_seconds=minimum,remaining_slack_seconds=decision.slack_seconds,resolution='kept'))
                    if previous and not decision.reachable and boundary in handled:
                        return route,replans,'Replacement boarding violates the original transfer requirements'
                    if leg.start_at<arrival:
                        return route,replans,'Corrected arrival is after boarding'
                    wait = _wait_leg(arrival,leg.start_at,leg.origin)
                    if wait:
                        result_legs.append(wait)
                leg.debug=s.LegDebug(traffic_status='not_applicable',service=service,
                    original_start_at=original_start,original_end_at=original_end)
            if leg.mode in SURFACE_MODES:
                segments=[]
                if graph is not None and leg.line:
                    try:
                        segments=run_bounded(lambda:_invoke(graph.trip_segments,leg.line.trip_id,service_day=None,
                            from_stop_id=leg.origin.stop_id,to_stop_id=leg.destination.stop_id,
                            feed_id=leg.mode if leg.mode in ('metro','rail') else 'gtfs'),deadline-.05,self.clock)
                    except Exception:
                        pass
                added=leg_eligible=leg_covered=leg_exempt=0.
                details=[]
                if not segments:
                    leg_eligible=leg.distance_meters
                    details.append(s.TrafficSegmentDebug(match_status='unavailable',lane_status='unknown',
                        rejection_reasons=['missing_baseline'],correction_status='unavailable',baseline_available=False))
                for segment in segments:
                    correction=_invoke(correct_segment,segment,observations,now=self.wall_clock(),deadline=deadline,clock=self.clock)
                    from .traffic_geometry import length
                    leg_exempt+=max(0,length(segment.geometry)-correction.applicable_m)
                    added+=correction.added_seconds
                    leg_eligible+=correction.applicable_m
                    leg_covered+=correction.covered_m
                    details.append(self._segment_debug(segment,correction,observations,leg.start_at))
                    for fragment in correction.fragments:
                        provider = fragment.get('provider') if isinstance(fragment,dict) else getattr(fragment,'provider',None)
                        if provider and correction.added_seconds>0:
                            providers.add(provider)
                if segments and any(len(x.geometry)<2 and getattr(x,'road_status','unknown') not in ('dedicated','segregated') for x in segments):
                    # OTP distance supplies only the unknown geometric remainder.
                    # Known GTFS paths and excluded lane spans retain their own
                    # metric length, independent of OTP's distance approximation.
                    known_length=sum(length(x.geometry) for x in segments)
                    leg_eligible+=max(0,leg.distance_meters-known_length)
                applicable+=leg_eligible;covered+=leg_covered
                if added:
                    applied={f['provider'] for c in details for f in c.fragments if f.get('added_seconds',0)>0}
                    leg.duration_source=next(iter(applied)) if len(applied)==1 else 'estimated'
                leg.duration_seconds+=math.ceil(added)
                leg.end_at=leg.start_at+timedelta(seconds=leg.duration_seconds)
                leg.debug.segments=details
                leg.debug.coverage=coverage(leg_eligible,leg_covered)
                leg.debug.traffic_status='corrected' if added else ('unchanged' if leg_eligible and leg_covered>=leg_eligible else 'partial')
                leg.debug.warnings.append('Unknown lane status: car estimate permitted with warning')
            elif leg.debug is None:
                leg.debug=s.LegDebug(traffic_status='not_applicable',original_start_at=original_start,original_end_at=original_end)
            result_legs.append(leg)
        for event in events:
            event.to_leg_index=next((i for i in range(event.from_leg_index+1,len(result_legs)) if result_legs[i].line),event.to_leg_index)
            if slack_unknown and event.status=='feasible': event.status='unknown'
        route.legs=result_legs
        recompute_route(route,request)
        route.provenance.traffic_providers=sorted(providers)
        route.debug=s.RouteDebug(evaluation_group=classify_coverage(applicable,covered,unverified or (slack_unknown and bool(events))),
            tail_replanning_status=tail_status,original_otp_route_id=original.id,original_rank=rank,
            original_duration_seconds=original.duration_seconds,corrected_duration_seconds=route.duration_seconds,
            added_delay_seconds=sum(max(0,l.duration_seconds-l.original_duration_seconds) for l in route.legs if l.mode in SURFACE_MODES),coverage=coverage(applicable,covered),
            transfer_status=('unknown' if slack_unknown else 'feasible') if events else 'not_applicable',transfers=events,
            ranking_reason='quality group, arrival, transfers, walking distance',
            warnings=(['Old frequency tail is reachable but unverified after required replanning failed'] if unverified else []) +
                (['Active OTP transfer requirements are unavailable; transfer feasibility is not fully verified'] if slack_unknown and events else []))
        return route,replans,None

    def _valid_tail(self,tail,previous,slack,graph,deadline):
        """OTP's stop-origin search has no preceding trip: check the boundary.

        The existing walk and GTFS minimum overlap; the minimum is never added
        a second time on top of the physical walking requirement.
        """
        first=next((leg for leg in tail.legs if leg.line),None)
        if first is None:
            return True
        walking=sum(leg.duration_seconds for leg in tail.legs[:tail.legs.index(first)] if leg.mode=='walk')
        minimum=walking+slack
        if graph is not None and hasattr(graph,'transfer_minimum'):
            try:
                info=run_bounded(lambda:_invoke(graph.transfer_minimum,previous.destination.stop_id,first.origin.stop_id,
                    from_trip_id=previous.line.trip_id,to_trip_id=first.line.trip_id),deadline-.05,self.clock)
                if info.get('forbidden'): return False
                minimum=max(minimum,info.get('minimum_seconds') or 0)
            except Exception:
                return False
        return (first.start_at-previous.end_at).total_seconds()>=minimum

    def _segment_debug(self, segment, correction, observations, entry):
        return s.TrafficSegmentDebug(segment_id=f'{segment.feed_id}:{segment.from_stop_id}:{segment.to_stop_id}:{segment.from_sequence}',
            from_stop_id=segment.from_stop_id,to_stop_id=segment.to_stop_id,
            match_status='matched' if correction.covered_m>=correction.applicable_m and correction.covered_m else 'partial' if correction.covered_m else 'unavailable',
            lane_spans=list(getattr(segment,'lane_spans',())),
            lane_source_url=getattr(segment,'lane_source_url',None),
            lane_source_observed_at=getattr(segment,'lane_source_observed_at',None),
            lane_status={'shared':'shared_road','dedicated':'dedicated_lane','segregated':'separate_track'}.get(getattr(segment,'road_status','unknown'),'unknown'),coverage=coverage(correction.applicable_m,correction.covered_m),
            baseline_kind='gtfs_schedule',baseline_gtfs_version=segment.gtfs_version,
            baseline_available=segment.moving_seconds is not None,
            baseline_duration_seconds=segment.moving_seconds,
            corrected_duration_seconds=(segment.moving_seconds+correction.added_seconds) if segment.moving_seconds is not None else None,
            added_delay_seconds=correction.added_seconds,
            correction_status='applied' if correction.added_seconds else 'not_needed' if correction.covered_m else 'unavailable',
            observations=self._observation_debug(correction,observations,entry),
            applied_provider=next((f['provider'] for f in correction.fragments if f.get('added_seconds',0)>0),None),
            rejection_reasons=list(dict.fromkeys(self._rejection_reason(r.get('reason')) for r in correction.rejected)),
            observation_ids=sorted({f['observation_id'] for f in correction.fragments}),
            fragments=correction.fragments,rejected_observations=correction.rejected,
            notes=list(correction.warnings)+(['road_status_version:'+segment.road_status_version] if getattr(segment,'road_status_version',None) else []))

    @staticmethod
    def _rejection_reason(reason):
        return {'shape_unavailable':'missing_geometry','missing_time_baseline':'missing_baseline',
            'no_unambiguous_oriented_overlap':'insufficient_overlap','zero_length':'missing_geometry'}.get(reason,
            reason if reason in ('ambiguous_geometry','stale_observation') else 'unknown')

    def _observation_debug(self,correction,observations,entry,include_all=False):
        now=self.wall_clock()
        selected={f['observation_id'] for f in correction.fragments}
        rejected={r.get('observation_id'):r.get('reason') for r in correction.rejected}
        relevant=selected | {oid for f in correction.fragments for oid in f.get('conflicting_observation_ids',[])}
        horizon=max(0,entry.timestamp()-now)
        return [s.TrafficObservationDebug(observation_id=o.id,provider=o.provider,traffic_type='realtime',
            observed_at=datetime.fromtimestamp(o.received_at,timezone.utc),
            expires_at=datetime.fromtimestamp(o.expires_at,timezone.utc),age_seconds=max(0,now-o.received_at),
            seconds_until_segment_entry=horizon,observed_age_at_entry_seconds=max(0,now-o.received_at)+horizon,
            cache_status='unknown',freshness='fresh' if o.fresh(now) else 'stale',
            driving_duration_seconds=o.duration_seconds,distance_meters=o.length_m,
            provider_alternative_id=str(o.alternative_index),provider_step_index=o.part_index,
            selected_for_correction=o.id in selected,selection_reason='geometry_error,freshness,provider,id' if o.id in selected else rejected.get(o.id,'lower_priority_or_not_overlapping'))
            for o in observations if include_all or o.id in relevant]
