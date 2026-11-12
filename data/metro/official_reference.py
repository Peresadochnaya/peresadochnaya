"""Export values actually published by the Saint Petersburg metro website."""

import csv
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from data.gtfs.refresh_static import CA_BUNDLE

INTERVAL_URL = "https://metro.spb.ru/intervalpoezd01092026.html"
HOURS_URL = "https://metro.spb.ru/rejimrabotystancii.html"
MAP_URL = "https://metro.spb.ru/map1/route.html"
GRAPH_URL = "https://metro.spb.ru/map1/files/spb00000.js"
COORD_URL = "https://api.hh.ru/metro/2"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "processed" / "metro"
DAY_TYPES = ("weekday", "saturday", "sunday")
LINE_CODES = {"kiv": 1, "mop": 2, "nev": 3, "prb": 4, "frp": 5, "kra": 6}
CLOSED_NAMES = {"mop-frn": "Фрунзенская", "mop-pap": "Парк Победы"}


def duration_seconds(value):
    minutes, seconds = map(int, value.split(":"))
    return minutes * 60 + seconds


def extract_intervals(html):
    soup = BeautifulSoup(html, "html.parser")
    if not re.search(r"с\s*01\.09\.2026", soup.get_text(" ", strip=True)):
        raise ValueError("Не подтверждена дата начала действия таблицы интервалов")
    tables = soup.find_all("table")
    if len(tables) != 6:
        raise ValueError(f"Ожидалось 6 линий, получено {len(tables)}")
    rows = []
    for line, table in enumerate(tables, 1):
        for tr in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"], recursive=False)]
            if len(cells) != 4 or not re.fullmatch(r"\d{1,2}:\d{2}\s*[–-]\s*\d{1,2}:\d{2}", cells[0]):
                continue
            period_start, period_end = re.split(r"\s*[–-]\s*", cells[0])
            for day_type, published in zip(DAY_TYPES, cells[1:]):
                values = re.findall(r"\d{1,2}:\d{2}", published)
                if not values:
                    raise ValueError(f"Не распознан интервал для линии {line}: {published}")
                rows.append({"line": line, "day_type": day_type, "period_start": period_start,
                             "period_end": period_end, "min_headway_seconds": duration_seconds(values[0]),
                             "max_headway_seconds": duration_seconds(values[-1]),
                             "source_valid_from": "20260901", "source_url": INTERVAL_URL})
    if len(rows) != 90:
        raise ValueError(f"Ожидалось 90 опубликованных интервалов, получено {len(rows)}")
    return rows


def extract_station_hours(html):
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True).replace("\xa0", " ")
    if not re.search(r"с\s*1\s*октября\s*2026", page_text):
        raise ValueError("Не подтверждена дата начала действия режима станций")
    tables = soup.find_all("table")
    if len(tables) < 2:
        raise ValueError("Не найдена таблица режима станций")
    rows, line, termini = [], None, None
    for tr in tables[1].find_all("tr"):
        cells = [cell.get_text(" ", strip=True).replace("\xa0", " ")
                 for cell in tr.find_all(["td", "th"], recursive=False)]
        if not cells:
            continue
        match = re.fullmatch(r"ЛИНИЯ\s+(\d+)", cells[0])
        if match:
            line, termini = int(match.group(1)), None
            continue
        if line and len(cells) == 4 and not cells[0].isdigit() and termini is None:
            termini = (cells[0], cells[1])
            continue
        if not cells[0].isdigit():
            continue
        if termini is None:
            raise ValueError(f"Не найдены конечные станции линии {line}")
        values = cells[2:]
        row = {"line": line, "number_on_page": cells[0], "station_or_vestibule": cells[1],
               "first_terminus": termini[0], "second_terminus": termini[1],
               "entry_exit_open": "", "entry_close": "", "exit_close": "",
               "first_to_first_terminus_odd": "", "first_to_first_terminus_even": "",
               "first_to_second_terminus_odd": "", "first_to_second_terminus_even": "",
               "last_to_first_terminus": "", "last_to_second_terminus": "",
               "status": "", "source_valid_from": "20261001", "source_url": HOURS_URL}
        if len(values) == 1:
            row["status"] = values[0]
        elif len(values) in (3, 9):
            keys = ("entry_exit_open", "entry_close", "exit_close",
                    "first_to_first_terminus_odd", "first_to_first_terminus_even",
                    "first_to_second_terminus_odd", "first_to_second_terminus_even",
                    "last_to_first_terminus", "last_to_second_terminus")
            row.update(zip(keys, values))
        else:
            raise ValueError(f"Неожиданные поля у станции {cells[1]}: {cells}")
        rows.append(row)
    return rows


def extract_network(map_html, graph_js, hh, *, closed_names=None, station_hours=None):
    """Export station order and connections without estimated durations."""
    if closed_names is None:
        closed_names = CLOSED_NAMES
    soup = BeautifulSoup(map_html, "html.parser")
    picker = soup.select_one("#stations ul.items") or soup.select_one("ul.items")
    id_match = re.search(r"stationsList\.init\((\[.*?\])\)", map_html, re.S)
    if picker is None or id_match is None:
        raise ValueError("Не найден список открытых станций на схеме")
    active_ids = json.loads(re.sub(r"/\*.*?\*/", "", id_match.group(1), flags=re.S))
    active_names = [item.get_text(" ", strip=True) for item in picker.find_all("li")]
    if len(active_ids) != len(active_names) or len(set(active_ids)) != len(active_ids):
        raise ValueError("Не совпали ID и названия станций на схеме")
    names = dict(zip(active_ids, active_names))
    graph_match = re.search(r"lines:\s*\{(.*?)\},\s*close:", graph_js, re.S)
    transfer_match = re.search(r"transfers:\s*\[(.*?)\]", graph_js, re.S)
    if graph_match is None or transfer_match is None:
        raise ValueError("Не найдены линии или пересадки на официальной схеме")
    graph = {}
    for code, content in re.findall(r"(\w+):\s*\[(.*?)\]", graph_match.group(1), re.S):
        graph[code] = [f"{code}-{station}" for station in re.findall(r'\{id:"([^"]+)"', content)]
    if set(graph) != set(LINE_CODES):
        raise ValueError("Набор линий на схеме изменился")
    graph_ids = {station for line in graph.values() for station in line}
    if graph_ids - set(names) != set(closed_names) or not set(names) <= graph_ids:
        raise ValueError("Набор открытых/закрытых станций изменился")
    names.update(closed_names)
    if station_hours is not None:
        closed_on_hours_page = {row["station_or_vestibule"] for row in station_hours if row["status"]}
        if not set(closed_names.values()) <= closed_on_hours_page:
            raise ValueError("Закрытые станции не подтверждены таблицей режима работы")
    hh_coords = {row["name"].casefold().replace("ё", "е"): (row["lat"], row["lng"])
                 for line in hh["lines"] for row in line["stations"]}
    stations, segments = [], []
    for code, ids in graph.items():
        for sequence, sid in enumerate(ids, 1):
            name = names[sid]
            search_name = name.replace("Площадь Ал. Невского", "Площадь Александра Невского")
            coords = hh_coords.get(search_name.casefold().replace("ё", "е"))
            if coords is None or not (59.5 < float(coords[0]) < 60.3 and 29.5 < float(coords[1]) < 31.0):
                raise ValueError(f"Нет координат станции {name}")
            stations.append({"station_id": f"metro:{sid}", "line": LINE_CODES[code],
                             "sequence": sequence, "name": name, "lat": coords[0], "lon": coords[1],
                             "boarding_allowed": 0 if sid in closed_names else 1,
                             "map_source_url": MAP_URL, "coordinate_source_url": COORD_URL})
            if sequence > 1:
                segments.append({"line": LINE_CODES[code], "from_station_id": f"metro:{ids[sequence - 2]}",
                                 "to_station_id": f"metro:{sid}", "source_url": GRAPH_URL})
    transfer_pairs = set()
    for a, b in re.findall(r'\{s1:"([^"]+)",s2:"([^"]+)"\}', transfer_match.group(1)):
        if a not in graph_ids or b not in graph_ids:
            raise ValueError(f"Пересадка на неизвестную станцию: {a}, {b}")
        transfer_pairs.add(tuple(sorted((f"metro:{a}", f"metro:{b}"))))
    transfers = [{"station_a_id": a, "station_b_id": b, "source_url": GRAPH_URL}
                 for a, b in sorted(transfer_pairs)]
    return stations, segments, transfers


def attach_station_ids(hours, stations):
    """Use the station's line and published name to join vestibule rows."""

    def key(line, name):
        name = re.sub(r"^Вестибюль\s+\d+\s+станции\s+", "", name, flags=re.I)
        name = re.sub(r"\s*\(.*", "", name)
        return (int(line), name.casefold().replace("ё", "е")
                .replace("площадь ал. невского", "площадь александра невского")
                .replace("-", " ").strip())

    index = {key(row["line"], row["name"]): row["station_id"] for row in stations}
    for row in hours:
        station_id = index.get(key(row["line"], row["station_or_vestibule"]))
        if station_id is None:
            raise ValueError(f"Не найдена станция для вестибюля {row['station_or_vestibule']}")
        row["station_id"] = station_id
    return hours


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    try:
        pages = {}
        for key, url in (("intervals", INTERVAL_URL), ("hours", HOURS_URL),
                         ("map", MAP_URL), ("graph", GRAPH_URL), ("coords", COORD_URL)):
            response = requests.get(url, timeout=(10, 30), verify=str(CA_BUNDLE) if key != "coords" else True,
                                    headers={"User-Agent": "Peresadochnaya/1.0 metro reference export"})
            response.raise_for_status()
            pages[key] = response.text
        intervals = extract_intervals(pages["intervals"])
        hours = extract_station_hours(pages["hours"])
        if len(hours) < 70:
            raise ValueError(f"Мало записей о станциях: {len(hours)}")
        stations, segments, transfers = extract_network(
            pages["map"], pages["graph"], json.loads(pages["coords"]), station_hours=hours)
        attach_station_ids(hours, stations)
        tables = {"intervals.csv": intervals, "station_hours.csv": hours, "stations.csv": stations,
                  "segments.csv": segments, "transfers.csv": transfers}
        for name, rows in tables.items():
            write_csv(OUTPUT_DIR / name, rows)
        manifest = {"schema_version": 1, "generated_at": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(),
                    "sources": [INTERVAL_URL, HOURS_URL, MAP_URL, GRAPH_URL, COORD_URL],
                    "files": {name: {"rows": len(rows),
                                     "sha256": hashlib.sha256((OUTPUT_DIR / name).read_bytes()).hexdigest()}
                              for name, rows in tables.items()}}
        (OUTPUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                                  encoding="utf-8")
        print(json.dumps({"interval_rows": len(intervals), "station_rows": len(hours),
                          "network_stations": len(stations), "segments": len(segments), "transfers": len(transfers),
                          "directory": str(OUTPUT_DIR)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, requests.RequestException) as exc:
        print(f"Ошибка получения официальных данных метро: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
