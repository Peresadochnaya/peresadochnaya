from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from google.transit import gtfs_realtime_pb2 as pb
import pytest

from realtime.worker import calculate, generate, prepare


@pytest.fixture
def db():
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.executescript('''CREATE TABLE gtfs_routes(route_id TEXT,route_type TEXT);
    INSERT INTO gtfs_routes VALUES ('r','3');
    CREATE TABLE gtfs_trips(trip_id TEXT,route_id TEXT,direction_id TEXT,shape_id TEXT);
    INSERT INTO gtfs_trips VALUES ('t','r','0','shape');
    CREATE TABLE gtfs_stops(stop_id TEXT,stop_lat TEXT,stop_lon TEXT);
    INSERT INTO gtfs_stops VALUES ('a','60','30'),('b','60','30.01'),('c','60','30.02');
    CREATE TABLE gtfs_stop_times(trip_id TEXT,stop_id TEXT,stop_sequence TEXT,arrival_time TEXT,departure_time TEXT);
    INSERT INTO gtfs_stop_times VALUES ('t','a','10','25:00:00','25:00:00'),('t','b','20','25:10:00','25:10:00'),('t','c','30','25:20:00','25:20:00');
    CREATE TABLE gtfs_shapes(shape_id TEXT,shape_pt_sequence TEXT,shape_pt_lat TEXT,shape_pt_lon TEXT);
    INSERT INTO gtfs_shapes VALUES ('shape','1','60','30'),('shape','2','60','30.02');
    CREATE TABLE catalogue(name TEXT);
    CREATE TABLE gtfs_frequencies(trip_id TEXT,exact_times TEXT);
    ''')
    yield db
    db.close()


def vehicle():
    v=pb.VehiclePosition();v.vehicle.id='vehicle';v.trip.trip_id='t';v.trip.route_id='r'
    v.position.latitude=60;v.position.longitude=30.005
    v.timestamp=int(datetime.fromisoformat('2026-11-13T01:07:00+03:00').timestamp())
    return v


def test_nonconsecutive_sequences_midnight_and_interpolated_delay(db):
    v=vehicle();job=prepare(db,v)
    assert job['day'].isoformat()=='2026-11-12'
    seq,delay,distance=calculate([job])[0]
    assert seq==20 and abs(delay-120)<1 and distance<1
    f=pb.FeedMessage();f.header.gtfs_realtime_version='2.0';f.header.timestamp=v.timestamp
    e=f.entity.add();e.id='e';e.vehicle.CopyFrom(v)
    result,stats=generate(f,db,now=v.timestamp)
    assert stats['trip_updates']==1 and stats['vehicles']==1
    update=result.entity[0].trip_update
    assert update.trip.start_date=='20261112'
    assert [s.stop_sequence for s in update.stop_time_update]==[20,30]
    assert update.stop_time_update[0].arrival.delay==120
    assert update.vehicle.id=='vehicle'


def test_far_from_route_unknown_trip_and_stale_are_not_invented(db):
    v=vehicle();job=prepare(db,v);v.position.latitude=61
    assert calculate([job])[0][0]==-1
    v.trip.trip_id='missing';assert prepare(db,v) is None
    f=pb.FeedMessage();f.header.gtfs_realtime_version='2.0';f.header.timestamp=v.timestamp
    e=f.entity.add();e.id='e';e.vehicle.CopyFrom(v)
    result,stats=generate(f,db,now=v.timestamp+100)
    assert stats['vehicles']==stats['trip_updates']==0


def test_frequency_requires_instance_and_uses_absolute_predictions(db):
    db.executescript("INSERT INTO catalogue VALUES ('frequencies.txt'); INSERT INTO gtfs_frequencies VALUES ('t','0');")
    v=vehicle();assert prepare(db,v) is None
    v.trip.start_time='25:00:00';v.trip.start_date='20261112'
    f=pb.FeedMessage();f.header.gtfs_realtime_version='2.0';f.header.timestamp=v.timestamp
    e=f.entity.add();e.id='e';e.vehicle.CopyFrom(v)
    result,_=generate(f,db,now=v.timestamp)
    update=result.entity[0].trip_update
    assert update.trip.schedule_relationship==pb.TripDescriptor.UNSCHEDULED
    assert update.stop_time_update[0].arrival.HasField('time')
    assert not update.stop_time_update[0].arrival.HasField('delay')


def test_negative_delay_and_route_mismatch(db):
    v=vehicle();v.timestamp-=240
    assert calculate([prepare(db,v)])[0][1]==-120
    v.trip.route_id='wrong';assert prepare(db,v) is None


def test_route_only_observation_is_visible_without_fabricated_trip_update(db):
    v=vehicle();v.trip.ClearField('trip_id')
    f=pb.FeedMessage();f.header.gtfs_realtime_version='2.0';f.header.timestamp=v.timestamp
    e=f.entity.add();e.id='e';e.vehicle.CopyFrom(v)
    result,stats=generate(f,db,now=v.timestamp)
    assert stats['vehicles']==1 and stats['trip_updates']==0
    assert len(result.entity)==1 and result.entity[0].HasField('vehicle')
