"""Join pMetro's reference running times to the official operating network."""

import configparser
import re

PMETRO_BASE = "https://raw.githubusercontent.com/pMetro/pMetro-maps"
PMETRO_REPO = "https://api.github.com/repos/pMetro/pMetro-maps/commits/master"


def name_key(value):
    value = value.casefold().replace("ё", "е")
    value = value.replace("площадь ал. невского", "площадь александра невского")
    value = re.sub(r"(?:-|\s+)(?:ii|i|[12])$", "", value)
    return re.sub(r"\s+", " ", value).strip()


def seconds(value):
    """pMetro durations use minutes.seconds, never decimal minutes."""
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        raise ValueError(f"Invalid pMetro duration: {value!r}")
    minutes, _, rest = value.partition(".")
    second = int(rest or "0")
    if second >= 60:
        raise ValueError(f"Invalid seconds: {value}")
    return int(minutes) * 60 + second


def split_items(value):
    """Separate items, preserving parenthesized branch lists and empty slots."""
    result, level, start = [], 0, 0
    for i, char in enumerate(value):
        level += (char == "(") - (char == ")")
        if level < 0:
            raise ValueError("Unbalanced pMetro branch")
        if char == "," and level == 0:
            result.append(value[start:i].strip())
            start = i + 1
    if level:
        raise ValueError("Unbalanced pMetro branch")
    return result + [value[start:].strip()]


def parse_pmetro(text):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.read_string(text)
    edges, transfers = {}, {}

    def put(target, key, duration):
        if duration <= 0:
            return  # Future stations have zero placeholders; never invent times.
        if key in target and target[key] != duration:
            raise ValueError(f"Conflicting pMetro times for {key}")
        target[key] = duration

    for section in config.sections():
        if not re.fullmatch(r"Line\d+", section):
            continue
        row = config[section]
        line = int(row["Name"])
        stations = split_items(row["Stations"])
        driving = split_items(row["Driving"])
        if len(driving) != len(stations) - 1:
            raise ValueError(f"Station/time count mismatch on line {line}")
        names = [name_key(item.split("(", 1)[0]) for item in stations]
        for i, raw in enumerate(driving):
            if "(" in stations[i]:
                branches = stations[i].split("(", 1)[1].removesuffix(")").split(",")
                if not raw.startswith("(") or not raw.endswith(")"):
                    raise ValueError("Missing branch durations")
                times = raw[1:-1].split(",")
                if len(branches) != len(times):
                    raise ValueError("Branch/time count mismatch")
                connections = zip(branches, times)
            else:
                connections = [(names[i + 1], raw)]
            for destination, duration in connections:
                if not destination.strip() or not duration.strip():
                    continue
                key = (line, *sorted((names[i], name_key(destination.strip()))))
                put(edges, key, seconds(duration.strip()))
    if config.has_section("Transfers"):
        for value in config["Transfers"].values():
            items = [item.strip() for item in value.split(",")]
            if len(items) != 5:
                raise ValueError("Unsupported pMetro transfer")
            a, b = (int(items[0]), name_key(items[1])), (int(items[2]), name_key(items[3]))
            put(transfers, tuple(sorted((a, b))), seconds(items[4]))
    if not edges:
        raise ValueError("pMetro has no running times")
    return edges, transfers


def attach_timings(tables, text, source_url):
    edges, changes = parse_pmetro(text)
    stations = {row["station_id"]: row for row in tables["stations.csv"]}
    segments, transfers = [], []
    for row in tables["segments.csv"]:
        a, b = stations[row["from_station_id"]], stations[row["to_station_id"]]
        key = (int(row["line"]), *sorted((name_key(a["name"]), name_key(b["name"]))))
        if key not in edges:
            raise ValueError(f"No positive running time for {a['name']} -> {b['name']}")
        segments.append({**row, "duration_seconds": edges[key], "duration_source": "pmetro",
                         "duration_source_url": source_url})
    for row in tables["transfers.csv"]:
        a, b = stations[row["station_a_id"]], stations[row["station_b_id"]]
        key = tuple(sorted(((int(a["line"]), name_key(a["name"])),
                            (int(b["line"]), name_key(b["name"])))))
        if key not in changes:
            raise ValueError(f"No positive transfer time for {a['name']} -> {b['name']}")
        transfers.append({**row, "duration_seconds": changes[key], "duration_source": "pmetro",
                          "duration_source_url": source_url})
    return {**tables, "segments.csv": segments, "transfers.csv": transfers}
