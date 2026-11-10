import unittest

from google.transit import gtfs_realtime_pb2

from data.gtfs.live_vehicles import build_snapshot


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "1.0"
        feed.header.timestamp = 1000

        trolley = feed.entity.add()
        trolley.id = "vehicle-1"
        trolley.vehicle.trip.route_id = "1087"
        trolley.vehicle.vehicle.id = "vehicle-1"
        trolley.vehicle.vehicle.label = "5101"
        trolley.vehicle.position.latitude = 60.0
        trolley.vehicle.position.longitude = 30.0
        trolley.vehicle.position.bearing = 90
        trolley.vehicle.position.speed = 10
        trolley.vehicle.timestamp = 995

        bus = feed.entity.add()
        bus.id = "vehicle-2"
        bus.vehicle.trip.route_id = "1347"
        bus.vehicle.vehicle.id = "vehicle-2"
        bus.vehicle.position.latitude = 59.0
        bus.vehicle.position.longitude = 31.0
        bus.vehicle.timestamp = 994

        self.payload = feed.SerializeToString()

    def test_filters_trolley_without_mixing_bus_with_same_public_number(self):
        snapshot = build_snapshot(self.payload, route_id="1087", fetched_at=1020)

        self.assertEqual(snapshot["feed_timestamp"], 1000)
        self.assertEqual(snapshot["feed_age_seconds"], 20)
        self.assertEqual(len(snapshot["vehicles"]), 1)
        self.assertEqual(snapshot["vehicles"][0]["route_id"], "1087")
        self.assertEqual(snapshot["vehicles"][0]["vehicle_id"], "vehicle-1")
        self.assertEqual(snapshot["vehicles"][0]["speed_mps"], 10.0)
        self.assertEqual(snapshot["vehicles"][0]["age_seconds"], 25)
        self.assertFalse(snapshot["vehicles"][0]["is_stale"])

    def test_all_routes_marks_old_vehicle_positions_stale(self):
        snapshot = build_snapshot(self.payload, route_id=None, fetched_at=1200)

        self.assertEqual(len(snapshot["vehicles"]), 2)
        self.assertEqual({v["route_id"] for v in snapshot["vehicles"]}, {"1087", "1347"})
        self.assertTrue(all(v["is_stale"] for v in snapshot["vehicles"]))


if __name__ == "__main__":
    unittest.main()
