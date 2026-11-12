import json
import unittest

from data.metro.official_reference import attach_station_ids, extract_intervals, extract_station_hours, extract_network


class OfficialReferenceTest(unittest.TestCase):
    def test_preserves_published_min_and_max_intervals(self):
        row = "<tr><td>5:45 – 8:00</td><td>1:45 – 8:00</td><td>2:30 – 8:00</td><td>2:30 – 8:00</td></tr>"
        table = "<table>" + row * 5 + "</table>"
        html = "<p>действующие с 01.09.2026</p>" + table * 6
        rows = extract_intervals(html)
        self.assertEqual(len(rows), 90)
        self.assertEqual(rows[0]["min_headway_seconds"], 105)
        self.assertEqual(rows[0]["max_headway_seconds"], 480)
        self.assertEqual(rows[0]["source_valid_from"], "20260901")

    def test_preserves_first_last_train_and_closed_station(self):
        html = """<p>с 1 октября 2026 года</p><table></table><table>
        <tr><td>ЛИНИЯ 1</td></tr><tr><td>№</td></tr>
        <tr><td>Девяткино</td><td>Проспект Ветеранов</td><td>Девяткино</td><td>Проспект Ветеранов</td></tr>
        <tr><td>нечет</td></tr>
        <tr><td>1</td><td>Автово</td><td>5:30</td><td>0:42</td><td>0:57</td>
        <td>5:35</td><td>5:35</td><td>5:49</td><td>5:49</td><td>0:12</td><td>0:47</td></tr>
        <tr><td>2</td><td>Парк Победы</td><td>Станция закрыта на реконструкцию</td></tr>
        </table>"""
        rows = extract_station_hours(html)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["first_to_first_terminus_odd"], "5:35")
        self.assertEqual(rows[0]["last_to_second_terminus"], "0:47")
        self.assertEqual(rows[1]["status"], "Станция закрыта на реконструкцию")

    def test_network_keeps_source_order_and_no_estimated_times(self):
        codes = ("kiv", "mop", "nev", "prb", "frp", "kra")
        ids = [f"{code}-aaa" for code in codes] + [f"{code}-bbb" for code in codes]
        names = [f"{code} A" for code in codes] + [f"{code} B" for code in codes]
        html = "<ul class='items'>" + "".join(f"<li>{name}</li>" for name in names) + "</ul>"
        html += "<script>stationsList.init(" + json.dumps(ids) + ");</script>"
        graph = "var subwayData={lines:{" + ",".join(
            f'{code}: [{{id:"aaa",x:0,y:0}},{{id:"bbb",x:1,y:1}}]'
            for code in codes
        ) + '},close:[],open:[],transfers:[{s1:"kiv-aaa",s2:"mop-aaa"},{s1:"mop-aaa",s2:"kiv-aaa"}]};'
        hh = {"lines": [{"stations": [{"name": name, "lat": 59.9, "lng": 30.3} for name in names]}]}
        stations, segments, transfers = extract_network(html, graph, hh, closed_names={})
        self.assertEqual(len(stations), 12)
        self.assertEqual(len(segments), 6)
        self.assertEqual(len(transfers), 1)
        self.assertEqual(stations[0]["station_id"], "metro:kiv-aaa")
        self.assertEqual(stations[1]["station_id"], "metro:kiv-bbb")
        self.assertFalse(any("time" in field or "duration" in field for field in segments[0]))

    def test_station_hours_join_by_line_and_name(self):
        rows = [{"line": 1, "station_or_vestibule": "Вестибюль 2 станции Площадь Восстания (выход на вокзал)"}]
        stations = [{"line": 1, "name": "Площадь Восстания", "station_id": "metro:kiv-plv"}]
        attach_station_ids(rows, stations)
        self.assertEqual(rows[0]["station_id"], "metro:kiv-plv")


if __name__ == "__main__":
    unittest.main()
