"""Planned diagnostics are explicit, optional and cannot imply measured traffic."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.otp import OtpPlanner
from app.schemas import (ProviderCallDebug, RoutesDebug, TrafficCoverage,
                         TrafficObservationDebug, TrafficSegmentDebug, TransferDebug, TransitServiceDebug, LegDebug)

REQUEST = {'points': [{'lat': 60.002, 'lon': 30.296}, {'lat': 60.036, 'lon': 30.445}],
           'departure_at': '2026-11-12T12:00:00+03:00'}


def test_existing_demo_has_no_claimed_traffic_diagnostics(tmp_path):
    with TestClient(create_app(tmp_path, 'dummy')) as client:
        data = client.post('/api/v1/routes', json=REQUEST).json()
    assert data['debug'] is None
    assert all(route['debug'] is None and all(leg['debug'] is None for leg in route['legs'])
               for route in data['routes'])
    assert data['sources']['traffic_providers'] == []


def test_existing_otp_adapter_does_not_fabricate_traffic_or_transfer_checks():
    fixture = Path(__file__).parents[1] / 'app/fixtures/otp/plan-response.json'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=json.loads(fixture.read_text())))
    from app.schemas import RouteRequest
    with httpx.Client(transport=transport) as client:
        result = OtpPlanner('http://otp:8080', client=client).plan(RouteRequest.model_validate(REQUEST))
    assert result.debug is None
    assert all(route.debug is None and all(leg.debug is None for leg in route.legs) for route in result.routes)
    assert result.sources.traffic_providers == []


def test_observed_provider_is_separate_from_applied_correction():
    now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    observation = TrafficObservationDebug(provider='yandex', traffic_type='realtime', cache_status='hit',
        freshness='fresh', observed_at=now, expires_at=now + timedelta(minutes=30), selected_for_correction=False)
    segment = TrafficSegmentDebug(match_status='rejected', lane_status='dedicated_lane',
        rejection_reasons=['dedicated_lane'], observations=[observation], correction_status='skipped')
    assert segment.applied_provider is None
    assert segment.corrected_duration_seconds is None
    assert segment.coverage is None
    assert segment.observations[0].selected_for_correction is False


def test_unknown_coverage_and_unchecked_transfer_are_not_successes():
    assert TrafficCoverage().coverage_ratio is None
    transfer = TransferDebug(from_leg_index=1, to_leg_index=4, status='not_checked', resolution='not_attempted')
    assert transfer.remaining_slack_seconds is None
    missed = transfer.model_copy(update={'status': 'missed', 'remaining_slack_seconds': -60})
    assert missed.remaining_slack_seconds == -60
    debug = RoutesDebug(pipeline_status='not_run')
    assert debug.cache_ttl_seconds is None and debug.candidate_count is None


@pytest.mark.parametrize('payload', [
    {'coverage_ratio': 1.1}, {'matched_distance_meters': -1}, {'eligible_distance_meters': 10, 'matched_distance_meters': 20},
    {'coverage_ratio': float('nan')}, {'uncovered_distance_meters': float('inf')},
])
def test_invalid_coverage_is_rejected(payload):
    with pytest.raises(ValidationError):
        TrafficCoverage.model_validate(payload)


def test_cache_expiry_and_no_raw_credentials_in_provider_call_contract():
    with pytest.raises(ValidationError, match='expires before'):
        TrafficObservationDebug(provider='2gis', traffic_type='realtime', cache_status='hit', freshness='fresh',
            observed_at='2026-11-12T12:00:00Z', expires_at='2026-11-12T11:59:59Z')
    with pytest.raises(ValidationError):
        ProviderCallDebug(provider='yandex', purpose='traffic_collection', status='succeeded',
                          request_url='https://example.invalid/?apikey=private')
    with pytest.raises(ValidationError):
        TrafficSegmentDebug(match_status='matched', lane_status='shared_road', correction_status='applied', correction_factor=0.5)


def test_exported_openapi_has_optional_debug_and_explains_missing_measurements(tmp_path):
    actual = create_app(tmp_path, 'dummy').openapi()
    expected = yaml.safe_load((Path(__file__).parents[2] / 'openapi.yaml').read_text())
    assert actual == expected
    schemas = actual['components']['schemas']
    for name in ('Leg', 'Route', 'RoutesResponse'):
        assert 'debug' not in schemas[name]['required']
        assert {'type': 'null'} in schemas[name]['properties']['debug']['anyOf']
    assert 'real traffic_mode=on' in schemas['Leg']['properties']['debug']['description']
    assert {'TrafficCoverage', 'TrafficSegmentDebug', 'TrafficObservationDebug', 'TransferDebug', 'ProviderCallDebug', 'RoutesDebug'} <= schemas.keys()
    assert 'not_checked' in schemas['TransferDebug']['properties']['status']['enum']
    assert 'rejection_reasons' in schemas['TrafficSegmentDebug']['properties']
    assert 'billable_units' in schemas['ProviderCallDebug']['properties']


def test_frequency_service_is_explicit_and_never_invents_exact_departure():
    service = TransitServiceDebug(service_kind='frequency', headway_seconds=300, frequency_exact_times=0,
                                  wait_model='half_headway')
    leg = LegDebug(traffic_status='not_evaluated', service=service)
    transfer = TransferDebug(from_leg_index=1, to_leg_index=4, status='unknown', resolution='not_attempted',
                             next_service=service)
    assert leg.service.frequency_exact_times == 0
    assert transfer.scheduled_departure_at is None
    assert transfer.next_service.wait_model == 'half_headway'
    assert TransitServiceDebug().service_kind is None
    assert transfer.status != 'feasible'


@pytest.mark.parametrize('field,value', [('headway_seconds', 0), ('frequency_exact_times', 2)])
def test_invalid_frequency_metadata_is_rejected(field, value):
    with pytest.raises(ValidationError):
        TransitServiceDebug.model_validate({field: value})


def test_cache_freshness_is_distinct_from_observation_horizon():
    observation = TrafficObservationDebug(provider='yandex', traffic_type='realtime', cache_status='hit',
        freshness='fresh', age_seconds=60, seconds_until_segment_entry=3600, observed_age_at_entry_seconds=3660)
    assert observation.freshness == 'fresh'
    assert observation.observed_age_at_entry_seconds > 1800


@pytest.mark.parametrize('field,value', [('lateral_distance_meters', -1), ('heading_difference_degrees', 181)])
def test_match_measurements_have_physical_bounds(field, value):
    with pytest.raises(ValidationError):
        TrafficSegmentDebug.model_validate({'match_status': 'not_checked', 'lane_status': 'unknown',
            'correction_status': 'not_evaluated', field: value})


def test_contract_documents_full_default_diagnostics_and_thirty_minute_cache(tmp_path):
    document = create_app(tmp_path, 'dummy').openapi()
    ttl = document['components']['schemas']['RoutesDebug']['properties']['cache_ttl_seconds']
    assert '1800 секунд (30 минут)' in ttl['description']
    assert 'обычного и демо-сценария' in ttl['description']
    assert 'без отдельного debug-флага' in document['info']['description']
    route_parameters = document['paths']['/api/v1/routes']['post'].get('parameters', [])
    assert 'debug' not in {parameter['name'] for parameter in route_parameters}
    observation = TrafficObservationDebug(provider='yandex', traffic_type='realtime', cache_status='hit',
        freshness='fresh', observed_at='2026-11-12T12:00:00Z', expires_at='2026-11-12T12:30:00Z')
    assert (observation.expires_at - observation.observed_at).total_seconds() == 1800


def test_unverified_tail_is_explicit_even_with_high_road_coverage():
    from app.schemas import RouteDebug
    quality = TrafficCoverage(eligible_distance_meters=1000, matched_distance_meters=900,
                              usable_distance_meters=900, usable_coverage_ratio=0.9)
    route = RouteDebug(evaluation_group='incomplete', tail_replanning_status='unverified_fallback',
        transfer_status='unknown', coverage=quality, warnings=['Tail replanning deadline reached; old reachable modeled tail retained unverified'])
    transfer = TransferDebug(from_leg_index=1, to_leg_index=4, status='unknown', resolution='kept_unverified',
        modeled_boarding_reachable=True, tail_replanning_required=True,
        next_service=TransitServiceDebug(service_kind='frequency', frequency_exact_times=0))
    assert route.evaluation_group == 'incomplete' and route.coverage.usable_coverage_ratio == 0.9
    assert transfer.resolution == 'kept_unverified' and transfer.scheduled_departure_at is None


def test_excluded_candidate_preserves_original_route_without_becoming_output(tmp_path):
    from app.schemas import ExcludedCandidateDebug, Route, RoutesResponse
    with TestClient(create_app(tmp_path, 'dummy')) as client:
        data = client.post('/api/v1/routes', json=REQUEST).json()
    original = Route.model_validate(data['routes'][0])
    excluded = ExcludedCandidateDebug(reason_code='boarding_unreachable', reason='Old modeled boarding unreachable; repair failed',
                                      original_route=original, tail_replanning_status='failed')
    data['routes'] = []
    data['debug'] = RoutesDebug(pipeline_status='partial', excluded_count=1, returned_count=0,
        excluded_candidates=[excluded], time_budget_seconds=15, deadline_reached=True).model_dump(mode='json')
    response = RoutesResponse.model_validate(data)
    assert response.routes == []
    assert response.debug.excluded_candidates[0].original_route.model_dump() == original.model_dump()
    assert response.debug.excluded_candidates[0].original_route.geometry == original.geometry
    assert response.debug.time_budget_seconds == 15


@pytest.mark.parametrize('group,warnings', [('sufficient', ['Unverified tail']), ('not_applicable', ['Unverified tail']), ('incomplete', [])])
def test_unverified_tail_cannot_claim_complete_quality_or_omit_warning(group, warnings):
    from app.schemas import RouteDebug
    with pytest.raises(ValidationError, match='Unverified fallback tail requires'):
        RouteDebug(evaluation_group=group, tail_replanning_status='unverified_fallback',
                   transfer_status='unknown', warnings=warnings)
