from datetime import datetime, timedelta, timezone
import pytest
from app.traffic_timing import classify_coverage, transfer_decision

@pytest.mark.parametrize('eligible,covered,unverified,expected',[(100,80,False,'sufficient'),(100,79.99,False,'incomplete'),(0,0,False,'not_applicable'),(0,0,True,'incomplete'),(100,100,True,'incomplete')])
def test_quality_groups(eligible,covered,unverified,expected):
    assert classify_coverage(eligible,covered,unverified)==expected

@pytest.mark.parametrize('frequency,delay,seconds,walking,reachable,replan',[(False,True,60,60,True,False),(False,True,59,60,False,True),(True,True,120,60,True,True),(True,False,120,60,True,False)])
def test_transfer_rules(frequency,delay,seconds,walking,reachable,replan):
    arrival=datetime(2026,11,12,tzinfo=timezone.utc)
    result=transfer_decision(arrival,arrival+timedelta(seconds=seconds),walking,frequency,delay)
    assert result.reachable==reachable
    assert result.replan==replan
