#!/usr/bin/env python3
import csv
import heapq
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "浙江杭州"
OUTPUT_DIR = ROOT / "outputs"
OSM_POI_FILE = OUTPUT_DIR / "hangzhou_osm_poi.json"
OSM_ROADS_FILE = OUTPUT_DIR / "hangzhou_osm_roads.json"
DATA_JSON = OUTPUT_DIR / "bus_route_map_data.json"
HTML_FILE = OUTPUT_DIR / "hangzhou_bus_route_map.html"

LON_FIELD = "经度"
LAT_FIELD = "纬度"
TIME_FIELD = "数据时间"
SPEED_FIELD = "车速 km/h"
CHARGE_FIELD = "充电状态"
SOC_FIELD = "SOC"
MILEAGE_FIELD = "累计里程 km"

ROUTE_SNAP_METERS = 60
CHARGE_CLUSTER_METERS = 160
GRID_SIZE_METERS = 120
ROAD_GRID_METERS = 110
ROAD_MATCH_MAX_METERS = 35
ROAD_CONNECT_MAX_AERIAL_METERS = 450
ROAD_CONNECT_MAX_PATH_METERS = 1200
ROAD_CONNECT_MAX_SEARCH_NODES = 3500
WAY_GAP_FILL_MAX_SEGMENTS = 80
WAY_GAP_FILL_MAX_METERS = 2500
RESIDENTIAL_MATCH_MAX_METERS = 18
LOW_CONFIDENCE_MIN_POINTS = 3
OSRM_MAX_POINTS = 100
OSRM_MAX_SEGMENTS_PER_ROUTE = 12
STOP_SPEED_KMH = 1.0
STOP_MIN_SECONDS = 20
STOP_MAX_SECONDS = 15 * 60
STOP_CLUSTER_METERS = 80
STOP_TO_BUS_STOP_METERS = 55
CONFIRMED_STOP_MIN_EVENTS = 3
CONFIRMED_STOP_MIN_DAYS = 2
TRAFFIC_SIGNAL_FILTER_METERS = 35

DISALLOWED_BUS_HIGHWAYS = {"service", "living_street", "pedestrian", "footway", "path", "cycleway"}
DISALLOWED_ACCESS = {"private", "no"}
ROAD_MATCH_PENALTY = {
    "motorway": 6,
    "trunk": 4,
    "primary": 0,
    "secondary": 0,
    "tertiary": 3,
    "unclassified": 8,
    "residential": 28,
    "motorway_link": 8,
    "trunk_link": 8,
    "primary_link": 6,
    "secondary_link": 6,
    "tertiary_link": 8,
}
ROAD_GRAPH_COST_MULTIPLIER = {
    "motorway": 1.15,
    "trunk": 1.05,
    "primary": 1.0,
    "secondary": 1.0,
    "tertiary": 1.15,
    "unclassified": 1.35,
    "residential": 3.0,
    "motorway_link": 1.2,
    "trunk_link": 1.15,
    "primary_link": 1.1,
    "secondary_link": 1.1,
    "tertiary_link": 1.2,
}


def parse_float(value, default=None):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def valid_coord(lon, lat):
    return lon is not None and lat is not None and 119.0 < lon < 121.5 and 29.0 < lat < 31.5


def haversine_m(a, b):
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


def project(lon, lat, ref_lat):
    return (
        lon * 111320 * math.cos(math.radians(ref_lat)),
        lat * 110540,
    )


def road_node_key(lat, lon):
    return f"{lat:.7f},{lon:.7f}"


def bus_road_allowed(tags):
    highway = tags.get("highway", "")
    access = tags.get("access", "")
    if highway in DISALLOWED_BUS_HIGHWAYS or access in DISALLOWED_ACCESS:
        return False
    if highway == "residential" and not tags.get("name"):
        return False
    return highway in ROAD_MATCH_PENALTY


def road_match_score(segment, dist):
    highway = segment.get("highway", "")
    if highway == "residential" and dist > RESIDENTIAL_MATCH_MAX_METERS:
        return None
    return dist + ROAD_MATCH_PENALTY.get(highway, 50)


def road_graph_cost(segment):
    return segment["length_m"] * ROAD_GRAPH_COST_MULTIPLIER.get(segment.get("highway", ""), 2.0)


def point_segment_distance_xy(px, py, segment):
    ax, ay = segment["x1"], segment["y1"]
    bx, by = segment["x2"], segment["y2"]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def read_csv_points(path):
    rows = []
    charging = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lon = parse_float(row.get(LON_FIELD))
            lat = parse_float(row.get(LAT_FIELD))
            if not valid_coord(lon, lat):
                continue
            speed = parse_float(row.get(SPEED_FIELD), 0)
            charge_state = str(row.get(CHARGE_FIELD, "")).strip()
            item = {
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "time": str(row.get(TIME_FIELD, "")).strip(),
                "speed": speed,
                "charge": charge_state,
                "soc": row.get(SOC_FIELD, ""),
                "mileage": row.get(MILEAGE_FIELD, ""),
            }
            rows.append(item)
            if charge_state in {"1", "4"}:
                charging.append(item)
    return rows, charging


def compress_consecutive_points(rows):
    compressed = []
    for point in rows:
        if compressed and compressed[-1]["lat"] == point["lat"] and compressed[-1]["lon"] == point["lon"]:
            compressed[-1]["count"] += 1
            compressed[-1]["end_time"] = point["time"]
            continue
        compressed.append({
            "lat": point["lat"],
            "lon": point["lon"],
            "count": 1,
            "start_time": point["time"],
            "end_time": point["time"],
        })
    return compressed


def build_poi_index(items, ref_lat, cell_m):
    grid = {}
    for item in items:
        x, y = project(item["lon"], item["lat"], ref_lat)
        key = (int(x // cell_m), int(y // cell_m))
        grid.setdefault(key, []).append((x, y, item))
    return grid


def nearest_indexed_item(point, grid, ref_lat, cell_m, max_m):
    x, y = project(point[1], point[0], ref_lat)
    ix = int(x // cell_m)
    iy = int(y // cell_m)
    best = (float("inf"), None)
    cells = int(math.ceil(max_m / cell_m)) + 1
    for dx in range(-cells, cells + 1):
        for dy in range(-cells, cells + 1):
            for item_x, item_y, item in grid.get((ix + dx, iy + dy), []):
                dist = math.hypot(x - item_x, y - item_y)
                if dist < best[0]:
                    best = (dist, item)
    if best[1] is None or best[0] > max_m:
        return None, None
    return best[1], best[0]


def extract_stop_events(rows, file_name):
    events = []
    current = None

    def finish():
        nonlocal current
        if not current:
            return
        start_dt = current["start_dt"]
        end_dt = current["end_dt"]
        duration = int((end_dt - start_dt).total_seconds()) if start_dt and end_dt else 0
        if STOP_MIN_SECONDS <= duration <= STOP_MAX_SECONDS and current["count"] >= 2:
            events.append({
                "file": file_name,
                "lat": round(current["lat_sum"] / current["count"], 6),
                "lon": round(current["lon_sum"] / current["count"], 6),
                "start_time": current["start_time"],
                "end_time": current["end_time"],
                "duration_s": duration,
                "count": current["count"],
            })
        current = None

    for row in rows:
        timestamp = parse_time(row["time"])
        is_stopped = row["speed"] is not None and row["speed"] <= STOP_SPEED_KMH and row["charge"] not in {"1", "4"}
        if not is_stopped or timestamp is None:
            finish()
            continue
        if current:
            gap_s = (timestamp - current["end_dt"]).total_seconds()
            center = (current["lat_sum"] / current["count"], current["lon_sum"] / current["count"])
            if gap_s > 90 or haversine_m((row["lat"], row["lon"]), center) > STOP_CLUSTER_METERS:
                finish()
        if not current:
            current = {
                "start_dt": timestamp,
                "end_dt": timestamp,
                "start_time": row["time"],
                "end_time": row["time"],
                "lat_sum": row["lat"],
                "lon_sum": row["lon"],
                "count": 1,
            }
        else:
            current["end_dt"] = timestamp
            current["end_time"] = row["time"]
            current["lat_sum"] += row["lat"]
            current["lon_sum"] += row["lon"]
            current["count"] += 1
    finish()
    return events


def cluster_points(points, radius_m, label_prefix):
    clusters = []
    for point in points:
        latlon = (point["lat"], point["lon"])
        chosen = None
        for cluster in clusters:
            center = (cluster["lat"], cluster["lon"])
            if haversine_m(latlon, center) <= radius_m:
                chosen = cluster
                break
        if chosen is None:
            chosen = {
                "name": f"{label_prefix}{len(clusters) + 1}",
                "lat": point["lat"],
                "lon": point["lon"],
                "count": 0,
                "first_time": point["time"],
                "last_time": point["time"],
                "charge_states": {},
                "files": set(),
            }
            clusters.append(chosen)
        chosen["count"] += 1
        chosen["lat"] = round((chosen["lat"] * (chosen["count"] - 1) + point["lat"]) / chosen["count"], 6)
        chosen["lon"] = round((chosen["lon"] * (chosen["count"] - 1) + point["lon"]) / chosen["count"], 6)
        chosen["last_time"] = point["time"]
        state = point.get("charge", "")
        chosen["charge_states"][state] = chosen["charge_states"].get(state, 0) + 1
        if "file" in point:
            chosen["files"].add(point["file"])
    for cluster in clusters:
        cluster["files"] = sorted(cluster["files"])
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


def load_osm_poi(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    pois = []
    for element in data.get("elements", []):
        tags = element.get("tags", {})
        lat = element.get("lat")
        lon = element.get("lon")
        if not valid_coord(lon, lat):
            continue
        highway = tags.get("highway", "")
        public_transport = tags.get("public_transport", "")
        if highway == "traffic_signals":
            kind = "traffic_signal"
        elif highway == "bus_stop" or public_transport in {"platform", "station"}:
            kind = "bus_stop"
        else:
            continue
        pois.append({
            "id": element.get("id"),
            "kind": kind,
            "lat": round(lat, 7),
            "lon": round(lon, 7),
            "name": tags.get("name") or tags.get("local_ref") or "",
            "tags": {k: v for k, v in tags.items() if k in {"highway", "public_transport", "bus", "name", "local_ref", "network"}},
        })
    return pois


def load_osm_roads(path, ref_lat):
    if not path.exists():
        raise FileNotFoundError(f"Missing OSM road data: {path}")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    segments = []
    for element in data.get("elements", []):
        if element.get("type") != "way":
            continue
        geometry = element.get("geometry") or []
        if len(geometry) < 2:
            continue
        tags = element.get("tags", {})
        highway = tags.get("highway", "")
        if not bus_road_allowed(tags):
            continue
        name = tags.get("name") or tags.get("ref") or ""
        way_id = element.get("id")
        for index in range(len(geometry) - 1):
            a = geometry[index]
            b = geometry[index + 1]
            lat1, lon1 = a.get("lat"), a.get("lon")
            lat2, lon2 = b.get("lat"), b.get("lon")
            if not valid_coord(lon1, lat1) or not valid_coord(lon2, lat2):
                continue
            if lat1 == lat2 and lon1 == lon2:
                continue
            x1, y1 = project(lon1, lat1, ref_lat)
            x2, y2 = project(lon2, lat2, ref_lat)
            start_node = road_node_key(round(lat1, 7), round(lon1, 7))
            end_node = road_node_key(round(lat2, 7), round(lon2, 7))
            segments.append({
                "id": f"{way_id}:{index}",
                "way_id": way_id,
                "index": index,
                "name": name,
                "highway": highway,
                "access": tags.get("access", ""),
                "service": tags.get("service", ""),
                "path": [[round(lat1, 7), round(lon1, 7)], [round(lat2, 7), round(lon2, 7)]],
                "start_node": start_node,
                "end_node": end_node,
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "length_m": round(math.hypot(x2 - x1, y2 - y1), 1),
            })
    return segments


def build_road_segment_index(segments):
    grid = {}
    for segment in segments:
        min_x = min(segment["x1"], segment["x2"]) - ROAD_MATCH_MAX_METERS
        max_x = max(segment["x1"], segment["x2"]) + ROAD_MATCH_MAX_METERS
        min_y = min(segment["y1"], segment["y2"]) - ROAD_MATCH_MAX_METERS
        max_y = max(segment["y1"], segment["y2"]) + ROAD_MATCH_MAX_METERS
        x0, x1 = int(min_x // ROAD_GRID_METERS), int(max_x // ROAD_GRID_METERS)
        y0, y1 = int(min_y // ROAD_GRID_METERS), int(max_y // ROAD_GRID_METERS)
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                grid.setdefault((gx, gy), []).append(segment)
    return grid


def build_way_segment_index(segments):
    way_segments = {}
    for segment in segments:
        way_segments.setdefault(segment["way_id"], []).append(segment)
    for items in way_segments.values():
        items.sort(key=lambda item: item["index"])
    return way_segments


def build_road_graph(segments):
    graph = {}
    for segment in segments:
        if segment.get("highway") == "residential":
            continue
        a = segment["start_node"]
        b = segment["end_node"]
        length = segment["length_m"]
        cost = road_graph_cost(segment)
        graph.setdefault(a, []).append((b, length, cost, segment["id"]))
        graph.setdefault(b, []).append((a, length, cost, segment["id"]))
    return graph


def nearest_road_segment(point, road_grid, ref_lat, max_m):
    px, py = project(point[1], point[0], ref_lat)
    ix = int(px // ROAD_GRID_METERS)
    iy = int(py // ROAD_GRID_METERS)
    cells = int(math.ceil(max_m / ROAD_GRID_METERS)) + 1
    best = (float("inf"), float("inf"), None)
    seen = set()
    for dx in range(-cells, cells + 1):
        for dy in range(-cells, cells + 1):
            for segment in road_grid.get((ix + dx, iy + dy), []):
                if segment["id"] in seen:
                    continue
                seen.add(segment["id"])
                dist = point_segment_distance_xy(px, py, segment)
                score = road_match_score(segment, dist)
                if score is None:
                    continue
                if score < best[0] or (score == best[0] and dist < best[1]):
                    best = (score, dist, segment)
    if best[2] is None or best[1] > max_m:
        return None, None
    return best[2], best[1]


def segment_endpoint_distance_m(left, right):
    pairs = [
        (left["path"][0], right["path"][0]),
        (left["path"][0], right["path"][1]),
        (left["path"][1], right["path"][0]),
        (left["path"][1], right["path"][1]),
    ]
    return min(haversine_m(a, b) for a, b in pairs)


def shortest_path_between_segments(start_segment, end_segment, graph):
    if start_segment["id"] == end_segment["id"]:
        return []
    start_nodes = [start_segment["start_node"], start_segment["end_node"]]
    end_nodes = {end_segment["start_node"], end_segment["end_node"]}
    queue = []
    best_cost = {}
    best_length = {}
    previous = {}
    for node in start_nodes:
        best_cost[node] = 0
        best_length[node] = 0
        heapq.heappush(queue, (0, 0, node))
    visited = 0
    found = None
    while queue and visited < ROAD_CONNECT_MAX_SEARCH_NODES:
        cost, path_length, node = heapq.heappop(queue)
        if cost != best_cost.get(node):
            continue
        visited += 1
        if path_length > ROAD_CONNECT_MAX_PATH_METERS:
            break
        if node in end_nodes:
            found = node
            break
        for next_node, edge_length, edge_cost, segment_id in graph.get(node, []):
            next_length = path_length + edge_length
            next_cost = cost + edge_cost
            if next_length > ROAD_CONNECT_MAX_PATH_METERS:
                continue
            if next_cost < best_cost.get(next_node, float("inf")):
                best_cost[next_node] = next_cost
                best_length[next_node] = next_length
                previous[next_node] = (node, segment_id)
                heapq.heappush(queue, (next_cost, next_length, next_node))
    if found is None:
        return None
    segment_ids = []
    node = found
    while node in previous:
        node, segment_id = previous[node]
        segment_ids.append(segment_id)
    segment_ids.reverse()
    return segment_ids


def finish_low_confidence_run(runs, current):
    if not current or current["points"] < LOW_CONFIDENCE_MIN_POINTS:
        return
    run = {
        "start_time": current["start_time"],
        "end_time": current["end_time"],
        "points": current["points"],
        "raw_records": current["raw_records"],
        "reason": current["reason"],
        "sample_points": current["sample_points"],
    }
    runs.append(run)


def add_osrm_sample(current, point):
    sample = current["sample_points"]
    if len(sample) < OSRM_MAX_POINTS:
        sample.append([point["lon"], point["lat"]])
        return
    replace_every = max(1, current["points"] // OSRM_MAX_POINTS)
    if current["points"] % replace_every == 0:
        sample[-1] = [point["lon"], point["lat"]]


def match_points_to_roads(points, file_name, road_grid, ref_lat):
    matched_by_segment = {}
    low_runs = []
    current_low = None
    matched_points = 0
    matched_records = 0
    unmatched_points = 0
    unmatched_records = 0
    max_distance = 0
    route_steps = []
    last_segment_id = None

    progress_step = 200_000
    for index, point in enumerate(points, start=1):
        if index == 1 or index % progress_step == 0:
            print(f"Matching {file_name}: {index:,}/{len(points):,}", flush=True)
        segment, dist = nearest_road_segment((point["lat"], point["lon"]), road_grid, ref_lat, ROAD_MATCH_MAX_METERS)
        if segment:
            finish_low_confidence_run(low_runs, current_low)
            current_low = None
            matched_points += 1
            matched_records += point["count"]
            max_distance = max(max_distance, dist)
            if segment["id"] != last_segment_id:
                route_steps.append({
                    "segment_id": segment["id"],
                    "time": point["start_time"],
                })
                last_segment_id = segment["id"]
            stats = matched_by_segment.setdefault(segment["id"], {
                "count": 0,
                "raw_records": 0,
                "first_time": point["start_time"],
                "last_time": point["end_time"],
                "max_distance_m": 0,
            })
            stats["count"] += 1
            stats["raw_records"] += point["count"]
            stats["first_time"] = min(stats["first_time"], point["start_time"])
            stats["last_time"] = max(stats["last_time"], point["end_time"])
            stats["max_distance_m"] = max(stats["max_distance_m"], round(dist, 1))
            continue

        unmatched_points += 1
        unmatched_records += point["count"]
        if current_low is None:
            current_low = {
                "start_time": point["start_time"],
                "end_time": point["end_time"],
                "points": 0,
                "raw_records": 0,
                "reason": "no_road_within_threshold",
                "sample_points": [],
            }
        current_low["end_time"] = point["end_time"]
        current_low["points"] += 1
        current_low["raw_records"] += point["count"]
        add_osrm_sample(current_low, point)
    finish_low_confidence_run(low_runs, current_low)

    match_rate = matched_points / len(points) if points else 0
    print(f"Matched {file_name}: {matched_points:,}/{len(points):,} ({match_rate:.1%})", flush=True)
    return {
        "file": file_name,
        "matched_by_segment": matched_by_segment,
        "route_steps": route_steps,
        "low_confidence_segments": low_runs,
        "summary": {
            "matched_points": matched_points,
            "matched_raw_records": matched_records,
            "unmatched_points": unmatched_points,
            "unmatched_raw_records": unmatched_records,
            "match_rate": round(match_rate, 4),
            "matched_segments": len(matched_by_segment),
            "route_steps": len(route_steps),
            "low_confidence_segments": len(low_runs),
            "max_local_distance_m": round(max_distance, 1),
        },
    }


def fill_same_way_continuity(matches_by_file, way_segments):
    fill_summary = {}
    for file_name, match in matches_by_file.items():
        matched_by_segment = match["matched_by_segment"]
        by_way = {}
        for segment_id in matched_by_segment:
            way_id_text, index_text = segment_id.split(":", 1)
            by_way.setdefault(int(way_id_text), []).append(int(index_text))

        filled = 0
        for way_id, matched_indexes in by_way.items():
            segments = way_segments.get(way_id, [])
            if not segments:
                continue
            if segments[0].get("highway") == "residential":
                continue
            segment_by_index = {segment["index"]: segment for segment in segments}
            for left, right in zip(sorted(set(matched_indexes)), sorted(set(matched_indexes))[1:]):
                gap = right - left
                if gap <= 1 or gap > WAY_GAP_FILL_MAX_SEGMENTS:
                    continue
                missing = [segment_by_index.get(index) for index in range(left + 1, right)]
                if not missing or any(segment is None for segment in missing):
                    continue
                missing_length = sum(segment["length_m"] for segment in missing)
                if missing_length > WAY_GAP_FILL_MAX_METERS:
                    continue
                left_stats = matched_by_segment.get(f"{way_id}:{left}", {})
                right_stats = matched_by_segment.get(f"{way_id}:{right}", {})
                first_time = min(left_stats.get("first_time", ""), right_stats.get("first_time", ""))
                last_time = max(left_stats.get("last_time", ""), right_stats.get("last_time", ""))
                for segment in missing:
                    stats = matched_by_segment.setdefault(segment["id"], {
                        "count": 0,
                        "raw_records": 0,
                        "first_time": first_time,
                        "last_time": last_time,
                        "max_distance_m": 0,
                        "continuity_fill": True,
                    })
                    if not stats.get("continuity_fill"):
                        continue
                    stats["continuity_fill"] = True
                    filled += 1
        match["summary"]["continuity_fill_segments"] = filled
        match["summary"]["drawn_segments"] = len(matched_by_segment)
        fill_summary[file_name] = filled
        print(f"Continuity fill {file_name}: {filled:,} road segments", flush=True)
    return fill_summary


def connect_route_steps_by_graph(matches_by_file, segments_by_id, road_graph):
    connect_summary = {}
    path_cache = {}
    for file_name, match in matches_by_file.items():
        matched_by_segment = match["matched_by_segment"]
        route_steps = match.get("route_steps", [])
        added = 0
        attempted = 0
        skipped_far = 0
        failed = 0
        for left_step, right_step in zip(route_steps, route_steps[1:]):
            left_id = left_step["segment_id"]
            right_id = right_step["segment_id"]
            if left_id == right_id:
                continue
            left = segments_by_id.get(left_id)
            right = segments_by_id.get(right_id)
            if not left or not right:
                continue
            if left["way_id"] == right["way_id"]:
                continue
            aerial = segment_endpoint_distance_m(left, right)
            if aerial > ROAD_CONNECT_MAX_AERIAL_METERS:
                skipped_far += 1
                continue
            attempted += 1
            key = (left_id, right_id)
            if key not in path_cache:
                path_cache[key] = shortest_path_between_segments(left, right, road_graph)
            path_ids = path_cache[key]
            if not path_ids:
                failed += 1
                continue
            for segment_id in path_ids:
                if segment_id in {left_id, right_id}:
                    continue
                segment = segments_by_id.get(segment_id)
                if not segment:
                    continue
                is_new = segment_id not in matched_by_segment
                stats = matched_by_segment.setdefault(segment_id, {
                    "count": 0,
                    "raw_records": 0,
                    "first_time": left_step["time"],
                    "last_time": right_step["time"],
                    "max_distance_m": 0,
                    "graph_connect": True,
                })
                if is_new:
                    added += 1
                stats["graph_connect"] = True
        match["summary"]["graph_connect_segments"] = added
        match["summary"]["graph_connect_attempts"] = attempted
        match["summary"]["graph_connect_failed"] = failed
        match["summary"]["graph_connect_skipped_far"] = skipped_far
        match["summary"]["drawn_segments"] = len(matched_by_segment)
        connect_summary[file_name] = {
            "added_segments": added,
            "attempts": attempted,
            "failed": failed,
            "skipped_far": skipped_far,
        }
        print(
            f"Graph connect {file_name}: {added:,} road segments, "
            f"{attempted:,} attempts, {failed:,} failed, {skipped_far:,} skipped far",
            flush=True,
        )
    return connect_summary


def build_route_vertex_index_from_segments(segment_ids, segments_by_id, ref_lat):
    grid = {}
    for segment_id in segment_ids:
        segment = segments_by_id.get(segment_id)
        if not segment:
            continue
        points = [
            segment["path"][0],
            segment["path"][1],
            [
                round((segment["path"][0][0] + segment["path"][1][0]) / 2, 7),
                round((segment["path"][0][1] + segment["path"][1][1]) / 2, 7),
            ],
        ]
        for lat, lon in points:
            x, y = project(lon, lat, ref_lat)
            key = (int(x // GRID_SIZE_METERS), int(y // GRID_SIZE_METERS))
            grid.setdefault(key, []).append((x, y))
    return grid


def nearest_route_vertex_m(point, grid, ref_lat):
    x, y = project(point[1], point[0], ref_lat)
    ix = int(x // GRID_SIZE_METERS)
    iy = int(y // GRID_SIZE_METERS)
    best = float("inf")
    cells = int(math.ceil(ROUTE_SNAP_METERS / GRID_SIZE_METERS)) + 1
    for dx in range(-cells, cells + 1):
        for dy in range(-cells, cells + 1):
            for rx, ry in grid.get((ix + dx, iy + dy), []):
                dist = math.hypot(x - rx, y - ry)
                if dist < best:
                    best = dist
    return best


def annotate_pois_by_routes(pois, route_grids_by_file, ref_lat):
    selected = []
    seen = set()
    for poi in pois:
        key = (poi["kind"], poi["id"])
        if key in seen:
            continue
        point = (poi["lat"], poi["lon"])
        files = []
        distances = {}
        for file_name, route_grid in route_grids_by_file.items():
            dist = nearest_route_vertex_m(point, route_grid, ref_lat)
            if dist <= ROUTE_SNAP_METERS:
                files.append(file_name)
                distances[file_name] = round(dist, 1)
        if files:
            poi = dict(poi)
            poi["files"] = sorted(files)
            poi["route_distances_m"] = dict(sorted(distances.items()))
            poi["distance_m"] = min(distances.values())
            poi["stop_files"] = []
            poi["candidate_stop_files"] = []
            poi["stop_stats"] = {}
            selected.append(poi)
            seen.add(key)
    selected.sort(key=lambda p: (p["kind"], p.get("name") or "", p["distance_m"]))
    return selected


def attach_stop_evidence(osm_pois, stop_events, all_bus_stops, traffic_signals, ref_lat):
    bus_stop_index = build_poi_index(all_bus_stops, ref_lat, STOP_TO_BUS_STOP_METERS)
    signal_index = build_poi_index(traffic_signals, ref_lat, TRAFFIC_SIGNAL_FILTER_METERS)
    evidence = {}
    for event in stop_events:
        station, dist = nearest_indexed_item(
            (event["lat"], event["lon"]),
            bus_stop_index,
            ref_lat,
            STOP_TO_BUS_STOP_METERS,
            STOP_TO_BUS_STOP_METERS,
        )
        if not station:
            continue
        signal, signal_dist = nearest_indexed_item(
            (event["lat"], event["lon"]),
            signal_index,
            ref_lat,
            TRAFFIC_SIGNAL_FILTER_METERS,
            TRAFFIC_SIGNAL_FILTER_METERS,
        )
        station_id = station["id"]
        by_file = evidence.setdefault(station_id, {})
        stats = by_file.setdefault(event["file"], {
            "events": 0,
            "total_dwell_s": 0,
            "max_dwell_s": 0,
            "first_time": event["start_time"],
            "last_time": event["end_time"],
            "nearest_stop_event_m": round(dist, 1),
            "near_traffic_signal_events": 0,
            "nearest_traffic_signal_m": None,
            "dates": set(),
        })
        stats["events"] += 1
        stats["total_dwell_s"] += event["duration_s"]
        stats["max_dwell_s"] = max(stats["max_dwell_s"], event["duration_s"])
        stats["first_time"] = min(stats["first_time"], event["start_time"])
        stats["last_time"] = max(stats["last_time"], event["end_time"])
        stats["nearest_stop_event_m"] = min(stats["nearest_stop_event_m"], round(dist, 1))
        event_date = event["start_time"][:10]
        if event_date:
            stats["dates"].add(event_date)
        if signal:
            stats["near_traffic_signal_events"] += 1
            rounded = round(signal_dist, 1)
            if stats["nearest_traffic_signal_m"] is None:
                stats["nearest_traffic_signal_m"] = rounded
            else:
                stats["nearest_traffic_signal_m"] = min(stats["nearest_traffic_signal_m"], rounded)

    poi_by_id = {poi["id"]: poi for poi in osm_pois if poi["kind"] == "bus_stop"}
    confirmed_count = 0
    candidate_count = 0
    for station_id, by_file in evidence.items():
        poi = poi_by_id.get(station_id)
        if not poi:
            continue
        normalized = {}
        confirmed_files = []
        candidate_files = []
        for file_name, stats in sorted(by_file.items()):
            service_days = len(stats["dates"])
            status = "confirmed"
            if stats["events"] < CONFIRMED_STOP_MIN_EVENTS or service_days < CONFIRMED_STOP_MIN_DAYS:
                status = "candidate"
            if status == "confirmed":
                confirmed_files.append(file_name)
            else:
                candidate_files.append(file_name)
            normalized[file_name] = {
                "events": stats["events"],
                "service_days": service_days,
                "total_dwell_s": stats["total_dwell_s"],
                "max_dwell_s": stats["max_dwell_s"],
                "first_time": stats["first_time"],
                "last_time": stats["last_time"],
                "nearest_stop_event_m": stats["nearest_stop_event_m"],
                "near_traffic_signal_events": stats["near_traffic_signal_events"],
                "nearest_traffic_signal_m": stats["nearest_traffic_signal_m"],
                "status": status,
            }
        poi["stop_files"] = confirmed_files
        poi["candidate_stop_files"] = candidate_files
        poi["stop_stats"] = normalized
        if confirmed_files:
            confirmed_count += 1
        if candidate_files and not confirmed_files:
            candidate_count += 1
    return {
        "stations_with_evidence": len(evidence),
        "confirmed_stations": confirmed_count,
        "candidate_only_stations": candidate_count,
    }


def osrm_match_segment(sample_points):
    if len(sample_points) < LOW_CONFIDENCE_MIN_POINTS:
        return None
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in sample_points[:OSRM_MAX_POINTS])
    radiuses = ";".join(["35"] * min(len(sample_points), OSRM_MAX_POINTS))
    query = urllib.parse.urlencode({
        "geometries": "geojson",
        "overview": "full",
        "radiuses": radiuses,
    })
    url = f"https://router.project-osrm.org/match/v1/driving/{coords}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "Codex local bus route map"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    if data.get("code") != "Ok" or not data.get("matchings"):
        return None
    best = max(data["matchings"], key=lambda item: item.get("confidence", 0))
    if best.get("confidence", 0) < 0.45:
        return None
    coordinates = best.get("geometry", {}).get("coordinates") or []
    path = [[round(lat, 7), round(lon, 7)] for lon, lat in coordinates if valid_coord(lon, lat)]
    if len(path) < 2:
        return None
    return {
        "path": path,
        "confidence": round(best.get("confidence", 0), 3),
        "source": "osrm",
    }


def refine_low_confidence_with_osrm(file_name, runs):
    if os.environ.get("USE_OSRM", "1") == "0":
        return [], {"enabled": False, "success": 0, "failed": 0, "skipped": len(runs), "reason": "USE_OSRM=0"}
    refined = []
    failed = 0
    skipped = max(0, len(runs) - OSRM_MAX_SEGMENTS_PER_ROUTE)
    for index, run in enumerate(runs[:OSRM_MAX_SEGMENTS_PER_ROUTE]):
        try:
            result = osrm_match_segment(run["sample_points"])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            result = None
        if not result:
            failed += 1
            continue
        refined.append({
            "id": f"osrm:{file_name}:{index}",
            "path": result["path"],
            "name": "OSRM 精修片段",
            "highway": "",
            "files": [file_name],
            "match_counts_by_file": {file_name: run["raw_records"]},
            "matched_points_by_file": {file_name: run["points"]},
            "source": "osrm",
            "refined_from": run["reason"],
            "confidence": result["confidence"],
        })
    return refined, {
        "enabled": True,
        "success": len(refined),
        "failed": failed,
        "skipped": skipped,
        "reason": "only low-confidence short segments are sent to OSRM",
    }


def build_road_segments_output(segments_by_id, matches_by_file, osrm_segments):
    records = []
    for segment_id, segment in segments_by_id.items():
        files = []
        counts = {}
        fill_files = []
        graph_files = []
        max_distance = 0
        for file_name, match in matches_by_file.items():
            stats = match["matched_by_segment"].get(segment_id)
            if not stats:
                continue
            files.append(file_name)
            counts[file_name] = stats["raw_records"]
            if stats.get("continuity_fill"):
                fill_files.append(file_name)
            if stats.get("graph_connect"):
                graph_files.append(file_name)
            max_distance = max(max_distance, stats["max_distance_m"])
        if not files:
            continue
        records.append({
            "id": segment_id,
            "path": segment["path"],
            "name": segment["name"],
            "highway": segment["highway"],
            "length_m": segment["length_m"],
            "files": sorted(files),
            "match_counts_by_file": dict(sorted(counts.items())),
            "source": "local_osm",
            "continuity_fill_files": sorted(fill_files),
            "graph_connect_files": sorted(graph_files),
            "max_distance_m": round(max_distance, 1),
        })
    records.extend(osrm_segments)
    records.sort(key=lambda item: (item["source"], item["id"]))
    return records


def build_html(data_json_name):
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>浙江杭州公交车行驶路径图</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map {{ height: 100%; margin: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .panel {{
      position: absolute;
      right: 12px;
      bottom: 24px;
      z-index: 900;
      width: min(390px, calc(100vw - 24px));
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid #d9dee7;
      border-radius: 8px;
      box-shadow: 0 12px 34px rgba(31, 41, 55, 0.18);
      color: #152033;
    }}
    .route-panel {{
      position: absolute;
      top: 84px;
      left: 12px;
      z-index: 1000;
      width: 230px;
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid #d9dee7;
      border-radius: 8px;
      box-shadow: 0 12px 34px rgba(31, 41, 55, 0.18);
      color: #152033;
    }}
    .panel header, .route-panel header {{
      padding: 12px 14px 8px;
      border-bottom: 1px solid #e5e9f0;
      font-weight: 700;
      font-size: 15px;
    }}
    .route-list {{
      padding: 10px 12px 12px;
      display: grid;
      gap: 8px;
      font-size: 13px;
    }}
    .route-toggle {{
      display: grid;
      grid-template-columns: 18px 1fr;
      gap: 8px;
      align-items: center;
      cursor: pointer;
      min-height: 24px;
    }}
    .route-toggle input {{
      width: 16px;
      height: 16px;
      margin: 0;
    }}
    .route-toggle span {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-width: 0;
    }}
    .stats {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      padding: 12px 14px;
      font-size: 12px;
    }}
    .stat {{
      border: 1px solid #e0e5ed;
      border-radius: 6px;
      padding: 8px;
      background: #fbfcfe;
    }}
    .stat strong {{ display: block; font-size: 17px; margin-bottom: 2px; }}
    .legend {{
      padding: 0 14px 12px;
      font-size: 12px;
      line-height: 1.85;
    }}
    .match-list {{
      margin-top: 6px;
      display: grid;
      gap: 3px;
      line-height: 1.45;
    }}
    .dot {{
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      margin-right: 6px;
      vertical-align: -1px;
    }}
    .route-chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin: 2px 6px 2px 0;
      white-space: nowrap;
    }}
    .line {{
      width: 22px;
      height: 4px;
      border-radius: 3px;
      display: inline-block;
    }}
    .leaflet-popup-content {{ font-size: 13px; line-height: 1.5; }}
    .leaflet-control-layers {{
      border: 1px solid #d9dee7 !important;
      border-radius: 8px !important;
      box-shadow: 0 8px 24px rgba(31, 41, 55, 0.12) !important;
    }}
    .ui-toggle {{
      position: absolute;
      left: 12px;
      bottom: 24px;
      z-index: 1100;
      min-width: 76px;
      min-height: 34px;
      border: 1px solid #cfd6e3;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.96);
      box-shadow: 0 8px 22px rgba(31, 41, 55, 0.16);
      color: #152033;
      cursor: pointer;
      font: 600 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .ui-toggle:hover {{
      background: #f8fafc;
    }}
    body.ui-hidden .route-panel,
    body.ui-hidden .panel {{
      display: none;
    }}
    @media (max-width: 680px) {{
      .route-panel {{
        top: 82px;
        left: 10px;
        width: min(230px, calc(100vw - 20px));
      }}
      .panel {{
        left: 10px;
        right: 10px;
        bottom: 62px;
        width: auto;
        max-height: 34vh;
        overflow: auto;
      }}
      .ui-toggle {{
        left: 10px;
        bottom: 18px;
      }}
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <button class="ui-toggle" id="uiToggle" type="button" aria-pressed="false">隐藏 UI</button>
  <aside class="route-panel">
    <header>CSV 轨迹</header>
    <div class="route-list" id="routeControls"></div>
  </aside>
  <aside class="panel">
    <header>浙江杭州公交车行驶路径图</header>
    <div class="stats" id="stats"></div>
    <div class="legend" id="legend"></div>
  </aside>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const colors = ["#2563eb", "#16a34a", "#dc2626", "#7c3aed", "#ea580c"];
    const map = L.map("map", {{ preferCanvas: true }});
    const base = L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);

    const routeLayers = L.layerGroup().addTo(map);
    const startEndLayers = L.layerGroup().addTo(map);
    const chargingLayer = L.layerGroup().addTo(map);
    const stationLayer = L.layerGroup().addTo(map);
    const signalLayer = L.layerGroup().addTo(map);
    const routeEndpointLayers = new Map();
    const linkedMarkerRecords = [];
    let activeFiles = new Set();
    const overlays = {{
      "道路化行驶路径": routeLayers,
      "起点 / 终点": startEndLayers,
      "充电位置": chargingLayer,
      "公交站点": stationLayer,
      "红绿灯": signalLayer
    }};
    L.control.layers({{"OpenStreetMap": base}}, overlays, {{ collapsed: false }}).addTo(map);

    function circle(lat, lon, color, radius, label, fill = color) {{
      return L.circleMarker([lat, lon], {{
        radius,
        color,
        weight: 2,
        fillColor: fill,
        fillOpacity: 0.88
      }}).bindPopup(label);
    }}

    const CanvasRouteLayer = L.Layer.extend({{
      initialize(segments, routes) {{
        this.segments = segments.map(segment => ({{
          ...segment,
          projected: [],
          bounds: null
        }}));
        this.routeColor = new Map(routes.map((route, index) => [route.file, colors[index % colors.length]]));
        this.visibleFiles = new Set(routes.map(route => route.file));
      }},
      setVisible(file, visible) {{
        if (visible) this.visibleFiles.add(file);
        else this.visibleFiles.delete(file);
        if (this.map) this.reset();
      }},
      onAdd(mapInstance) {{
        this.map = mapInstance;
        this.canvas = L.DomUtil.create("canvas", "leaflet-zoom-animated");
        this.ctx = this.canvas.getContext("2d");
        mapInstance.getPanes().overlayPane.appendChild(this.canvas);
        mapInstance.on("move zoom resize zoomend", this.reset, this);
        mapInstance.on("click", this.handleClick, this);
        this.reset();
      }},
      onRemove(mapInstance) {{
        mapInstance.off("move zoom resize zoomend", this.reset, this);
        mapInstance.off("click", this.handleClick, this);
        this.canvas.remove();
      }},
      segmentActiveFiles(segment) {{
        return (segment.files || []).filter(file => this.visibleFiles.has(file));
      }},
      reset() {{
        const size = this.map.getSize();
        const topLeft = this.map.containerPointToLayerPoint([0, 0]);
        L.DomUtil.setPosition(this.canvas, topLeft);
        const scale = window.devicePixelRatio || 1;
        this.canvas.width = size.x * scale;
        this.canvas.height = size.y * scale;
        this.canvas.style.width = `${{size.x}}px`;
        this.canvas.style.height = `${{size.y}}px`;
        const ctx = this.ctx;
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        ctx.clearRect(0, 0, size.x, size.y);
        ctx.lineCap = "round";
        ctx.lineJoin = "round";

        const drawSegment = (segment, strokeStyle, lineWidth, alpha) => {{
          const pts = segment.path.map(([lat, lon]) => this.map.latLngToContainerPoint([lat, lon]));
          segment.projected = pts;
          if (pts.length < 2) return;
          ctx.beginPath();
          ctx.strokeStyle = strokeStyle;
          ctx.lineWidth = lineWidth;
          ctx.globalAlpha = alpha;
          pts.forEach((pt, index) => index === 0 ? ctx.moveTo(pt.x, pt.y) : ctx.lineTo(pt.x, pt.y));
          ctx.stroke();
        }};

        const activeSegments = this.segments
          .map(segment => [segment, this.segmentActiveFiles(segment)])
          .filter(([, files]) => files.length);
        for (const [segment] of activeSegments) {{
          drawSegment(segment, "rgba(255, 255, 255, 0.95)", segment.source === "osrm" ? 8 : 6.4, 0.82);
        }}
        for (const [segment, files] of activeSegments) {{
          const color = files.includes("01.csv") ? this.routeColor.get("01.csv") : this.routeColor.get(files[0]);
          const width = segment.source === "osrm" ? 4.8 : (files.includes("01.csv") ? 4.4 : 3.4);
          drawSegment(segment, color || "#2563eb", width, segment.source === "osrm" ? 0.96 : 0.86);
        }}
        ctx.globalAlpha = 1;
      }},
      handleClick(event) {{
        const click = this.map.latLngToContainerPoint(event.latlng);
        let best = null;
        for (const segment of this.segments) {{
          const files = this.segmentActiveFiles(segment);
          if (!files.length || !segment.projected || segment.projected.length < 2) continue;
          for (let i = 0; i < segment.projected.length - 1; i++) {{
            const dist = pointLineDistance(click, segment.projected[i], segment.projected[i + 1]);
            if (dist <= 8 && (!best || dist < best.dist)) best = {{ segment, files, dist }};
          }}
        }}
        if (!best) return;
        const segment = best.segment;
        const counts = Object.entries(segment.match_counts_by_file || {{}})
          .filter(([file]) => best.files.includes(file))
          .map(([file, count]) => `<code>${{escapeHtml(file)}}</code>：${{Number(count || 0).toLocaleString()}} 条`)
          .join("<br>");
        L.popup()
          .setLatLng(event.latlng)
          .setContent(`<strong>道路化轨迹</strong><br>道路名：${{escapeHtml(segment.name || "未命名道路")}}<br>道路等级：${{escapeHtml(segment.highway || "未知")}}<br>来源：${{segment.source === "osrm" ? "OSRM 精修" : "本地 OSM 精确匹配"}}${{(segment.graph_connect_files || []).some(file => best.files.includes(file)) ? "，路网连接" : ""}}${{(segment.continuity_fill_files || []).some(file => best.files.includes(file)) ? "，同路段补齐" : ""}}<br>关联 CSV：${{formatFiles(best.files)}}<br>匹配记录：<br>${{counts || "连接补齐路段"}}`)
          .openOn(this.map);
      }}
    }});

    function pointLineDistance(p, a, b) {{
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      if (dx === 0 && dy === 0) return Math.hypot(p.x - a.x, p.y - a.y);
      const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy)));
      const cx = a.x + t * dx;
      const cy = a.y + t * dy;
      return Math.hypot(p.x - cx, p.y - cy);
    }}

    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>"']/g, ch => ({{
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
      }}[ch]));
    }}

    function formatDuration(seconds) {{
      const total = Number(seconds || 0);
      const minutes = Math.floor(total / 60);
      const rest = total % 60;
      return minutes > 0 ? `${{minutes}}分${{rest}}秒` : `${{rest}}秒`;
    }}

    function formatPercent(value) {{
      return `${{(Number(value || 0) * 100).toFixed(1)}}%`;
    }}

    function formatFiles(files) {{
      return (files || []).map(file => `<code>${{escapeHtml(file)}}</code>`).join("、") || "无";
    }}

    function hasActiveFile(files) {{
      return (files || []).some(file => activeFiles.has(file));
    }}

    function addLinkedMarker(group, marker, files) {{
      const record = {{ group, marker, files: files || [] }};
      linkedMarkerRecords.push(record);
      if (hasActiveFile(record.files)) marker.addTo(group);
    }}

    function refreshLinkedMarkers() {{
      linkedMarkerRecords.forEach(record => {{
        const shouldShow = hasActiveFile(record.files);
        const isShown = record.group.hasLayer(record.marker);
        if (shouldShow && !isShown) record.marker.addTo(record.group);
        if (!shouldShow && isShown) record.group.removeLayer(record.marker);
      }});
    }}

    function setupUiToggle() {{
      const button = document.getElementById("uiToggle");
      button.addEventListener("click", () => {{
        const hidden = document.body.classList.toggle("ui-hidden");
        button.textContent = hidden ? "显示 UI" : "隐藏 UI";
        button.setAttribute("aria-pressed", String(hidden));
      }});
    }}

    function buildRouteControls(routes, canvasRoutes) {{
      const container = document.getElementById("routeControls");
      container.innerHTML = routes.map((route, index) => `
        <label class="route-toggle">
          <input type="checkbox" data-file="${{escapeHtml(route.file)}}" checked>
          <span><span class="line" style="background:${{colors[index % colors.length]}}"></span>${{escapeHtml(route.file)}} · ${{formatPercent(route.road_match.match_rate)}}</span>
        </label>
      `).join("");
      container.querySelectorAll("input[type='checkbox']").forEach(input => {{
        input.addEventListener("change", event => {{
          const file = event.target.dataset.file;
          const visible = event.target.checked;
          canvasRoutes.setVisible(file, visible);
          if (visible) activeFiles.add(file);
          else activeFiles.delete(file);
          const endpointLayer = routeEndpointLayers.get(file);
          if (endpointLayer) {{
            if (visible) endpointLayer.addTo(startEndLayers);
            else startEndLayers.removeLayer(endpointLayer);
          }}
          refreshLinkedMarkers();
        }});
      }});
    }}

    setupUiToggle();

    fetch("{data_json_name}")
      .then(response => response.json())
      .then(data => {{
        const bounds = [];
        activeFiles = new Set(data.routes.map(route => route.file));
        const canvasRoutes = new CanvasRouteLayer(data.road_segments || [], data.routes).addTo(routeLayers);
        data.routes.forEach((route, index) => {{
          bounds.push([route.start.lat, route.start.lon], [route.end.lat, route.end.lon]);
          const endpointLayer = L.layerGroup();
          circle(route.start.lat, route.start.lon, "#0f766e", 8, `<strong>${{escapeHtml(route.file)}} 起点</strong><br>${{escapeHtml(route.start.time)}}<br>SOC：${{escapeHtml(route.start.soc)}}`).addTo(endpointLayer);
          circle(route.end.lat, route.end.lon, "#991b1b", 8, `<strong>${{escapeHtml(route.file)}} 终点</strong><br>${{escapeHtml(route.end.time)}}<br>SOC：${{escapeHtml(route.end.soc)}}`).addTo(endpointLayer);
          endpointLayer.addTo(startEndLayers);
          routeEndpointLayers.set(route.file, endpointLayer);
        }});
        buildRouteControls(data.routes, canvasRoutes);
        if (data.summary.bbox) {{
          const [minLon, minLat, maxLon, maxLat] = data.summary.bbox;
          bounds.push([minLat, minLon], [maxLat, maxLon]);
        }}

        data.charging_locations.forEach(item => {{
          const marker = circle(item.lat, item.lon, "#f59e0b", Math.min(12, 5 + Math.log10(item.count + 1) * 2.8),
            `<strong>充电位置</strong><br>${{escapeHtml(item.name)}}<br>关联 CSV：${{formatFiles(item.files)}}<br>记录数：${{item.count.toLocaleString()}}<br>状态：${{escapeHtml(JSON.stringify(item.charge_states))}}<br>${{escapeHtml(item.first_time)}} 至 ${{escapeHtml(item.last_time)}}`,
            "#fde68a");
          addLinkedMarker(chargingLayer, marker, item.files);
        }});

        data.osm_pois.filter(p => p.kind === "bus_stop").forEach(item => {{
          const hasConfirmed = item.stop_files && item.stop_files.length;
          const hasCandidate = item.candidate_stop_files && item.candidate_stop_files.length;
          const statsHtml = Object.entries(item.stop_stats || {{}}).map(([file, stats]) =>
            `<br><code>${{escapeHtml(file)}}</code>：${{stats.status === "confirmed" ? "正式" : "候选"}}，停靠 ${{stats.events}} 次，跨 ${{stats.service_days}} 天，累计 ${{formatDuration(stats.total_dwell_s)}}，最长 ${{formatDuration(stats.max_dwell_s)}}${{stats.nearest_traffic_signal_m !== null ? `，近红绿灯 ${{stats.nearest_traffic_signal_m}}m` : ""}}`
          ).join("") || "<br>停靠证据：未达到重复停靠阈值";
          const label = hasConfirmed ? "正式停靠站" : (hasCandidate ? "候选站点" : "沿线站点");
          const marker = circle(item.lat, item.lon, hasConfirmed ? "#1d4ed8" : (hasCandidate ? "#ca8a04" : "#64748b"), hasConfirmed ? 5.8 : (hasCandidate ? 4.8 : 3.4),
            `<strong>${{label}}</strong><br>${{escapeHtml(item.name || "未命名站点")}}<br>轨迹关联：${{formatFiles(item.files)}}<br>正式归属：${{formatFiles(item.stop_files)}}<br>候选归属：${{formatFiles(item.candidate_stop_files)}}${{statsHtml}}`,
            hasConfirmed ? "#60a5fa" : (hasCandidate ? "#fde047" : "#cbd5e1"));
          addLinkedMarker(stationLayer, marker, item.files);
        }});
        data.osm_pois.filter(p => p.kind === "traffic_signal").forEach(item => {{
          const marker = circle(item.lat, item.lon, "#dc2626", 3.8,
            `<strong>红绿灯</strong><br>${{escapeHtml(item.name || "交通信号灯")}}<br>关联 CSV：${{formatFiles(item.files)}}<br>最近道路化轨迹距离：${{item.distance_m}} m`,
            "#fecaca");
          addLinkedMarker(signalLayer, marker, item.files);
        }});

        if (bounds.length) {{
          map.fitBounds(bounds, {{ padding: [28, 28] }});
        }}

        const lowQuality = data.routes.filter(route => route.road_match.refined_match_rate < 0.9);
        const matchRows = data.routes.map(route =>
          `<div><code>${{escapeHtml(route.file)}}</code> 公交道路匹配 ${{formatPercent(route.road_match.match_rate)}}，同路补齐 ${{route.road_match.continuity_fill_segments || 0}} 段，路网连接 ${{route.road_match.graph_connect_segments || 0}} 段，低置信 ${{route.road_match.low_confidence_segments}} 段${{route.road_match.refined_match_rate < 0.9 ? "，需要人工/API 精修" : ""}}</div>`
        ).join("");
        document.getElementById("stats").innerHTML = `
          <div class="stat"><strong>${{data.routes.length}}</strong>CSV 轨迹</div>
          <div class="stat"><strong>${{data.summary.total_raw_points.toLocaleString()}}</strong>原始定位点</div>
          <div class="stat"><strong>${{data.road_segments.length.toLocaleString()}}</strong>道路化片段</div>
          <div class="stat"><strong>${{data.osm_counts.confirmed_stop_bus_stop || 0}}</strong>正式站 / <strong style="display:inline">${{data.osm_counts.traffic_signal}}</strong>灯</div>
        `;
        document.getElementById("legend").innerHTML = `
          <div>${{data.routes.map((route, index) => `<span class="route-chip"><span class="line" style="background:${{colors[index % colors.length]}}"></span>${{escapeHtml(route.file)}}</span>`).join("")}}</div>
          <div>主轨迹已道路化：35m 公交道路吸附为主，排除小区/服务/私有道路，并用 OSM 路网连接相邻道路段；原始 GPS 折线不在主视图绘制。</div>
          <div class="match-list">${{matchRows}}</div>
          <div><span class="dot" style="background:#0f766e"></span>起点 <span class="dot" style="background:#991b1b;margin-left:12px"></span>终点 <span class="dot" style="background:#f59e0b;margin-left:12px"></span>充电位置</div>
          <div><span class="dot" style="background:#60a5fa"></span>正式站 <span class="dot" style="background:#fde047;margin-left:12px"></span>候选站 <span class="dot" style="background:#dc2626;margin-left:12px"></span>红绿灯</div>
          <div>正式站阈值：同一 CSV 同一 OSM 站点至少 ${{data.summary.confirmed_stop_min_events}} 次、跨至少 ${{data.summary.confirmed_stop_min_days}} 天；靠近红绿灯但缺少重复规律的停车只保留为候选。</div>
          <div>CSV 勾选会联动隐藏/显示对应道路轨迹、充电位置、站点、红绿灯。${{lowQuality.length ? " 有线路精修后仍低于 90%，建议后续接入高德/百度/Google API 深度精修。" : ""}}</div>
        `;
      }})
      .catch(error => {{
        console.error(error);
        document.getElementById("stats").innerHTML = `<div class="stat"><strong>错误</strong>数据加载失败</div>`;
      }});
  </script>
</body>
</html>
"""


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    csv_payloads = []
    charging_points = []
    stop_events = []
    total_raw_points = 0
    total_compressed_points = 0
    total_merged_repeats = 0
    bbox = [999, 999, -999, -999]

    for path in sorted(DATA_DIR.glob("*.csv")):
        print(f"Reading {path.name}", flush=True)
        rows, charging = read_csv_points(path)
        if not rows:
            continue
        compressed = compress_consecutive_points(rows)
        for point in charging:
            point["file"] = path.name
        charging_points.extend(charging)
        total_raw_points += len(rows)
        total_compressed_points += len(compressed)
        total_merged_repeats += len(rows) - len(compressed)
        stop_events.extend(extract_stop_events(rows, path.name))
        for p in rows:
            bbox[0] = min(bbox[0], p["lon"])
            bbox[1] = min(bbox[1], p["lat"])
            bbox[2] = max(bbox[2], p["lon"])
            bbox[3] = max(bbox[3], p["lat"])
        csv_payloads.append({
            "file": path.name,
            "rows": rows,
            "compressed": compressed,
        })
        print(f"Loaded {path.name}: {len(rows):,} rows, {len(compressed):,} compressed", flush=True)

    if not csv_payloads:
        raise RuntimeError(f"No CSV files found in {DATA_DIR}")

    ref_lat = (bbox[1] + bbox[3]) / 2
    road_segments = load_osm_roads(OSM_ROADS_FILE, ref_lat)
    segments_by_id = {segment["id"]: segment for segment in road_segments}
    way_segments = build_way_segment_index(road_segments)
    road_graph = build_road_graph(road_segments)
    road_grid = build_road_segment_index(road_segments)
    matches_by_file = {}
    route_grids_by_file = {}
    routes = []
    osrm_segments = []
    osrm_summary_by_file = {}

    for payload in csv_payloads:
        file_name = payload["file"]
        match = match_points_to_roads(payload["compressed"], file_name, road_grid, ref_lat)
        refined_segments, osrm_summary = refine_low_confidence_with_osrm(file_name, match["low_confidence_segments"])
        osrm_segments.extend(refined_segments)
        osrm_summary_by_file[file_name] = osrm_summary
        refined_points = sum(segment.get("matched_points_by_file", {}).get(file_name, 0) for segment in refined_segments)
        refined_rate = (match["summary"]["matched_points"] + refined_points) / len(payload["compressed"])
        match["summary"]["refined_match_rate"] = round(min(1, refined_rate), 4)
        match["summary"]["osrm_refined_segments"] = osrm_summary["success"]
        match["summary"]["osrm_failed_segments"] = osrm_summary["failed"]
        match["summary"]["osrm_skipped_segments"] = osrm_summary["skipped"]
        matches_by_file[file_name] = match
        rows = payload["rows"]
        routes.append({
            "file": file_name,
            "raw_count": len(rows),
            "compressed_count": len(payload["compressed"]),
            "merged_repeated_points": len(rows) - len(payload["compressed"]),
            "start": rows[0],
            "end": rows[-1],
            "road_match": match["summary"],
            "osrm_refinement": osrm_summary,
        })

    continuity_fill_by_file = fill_same_way_continuity(matches_by_file, way_segments)
    graph_connect_by_file = connect_route_steps_by_graph(matches_by_file, segments_by_id, road_graph)
    for route in routes:
        summary = matches_by_file[route["file"]]["summary"]
        route["road_match"]["continuity_fill_segments"] = summary["continuity_fill_segments"]
        route["road_match"]["graph_connect_segments"] = summary["graph_connect_segments"]
        route["road_match"]["graph_connect_attempts"] = summary["graph_connect_attempts"]
        route["road_match"]["graph_connect_failed"] = summary["graph_connect_failed"]
        route["road_match"]["graph_connect_skipped_far"] = summary["graph_connect_skipped_far"]
        route["road_match"]["drawn_segments"] = summary["drawn_segments"]
    route_grids_by_file = {
        file_name: build_route_vertex_index_from_segments(match["matched_by_segment"], segments_by_id, ref_lat)
        for file_name, match in matches_by_file.items()
    }

    charging_locations = cluster_points(charging_points, CHARGE_CLUSTER_METERS, "充电位置")
    pois = load_osm_poi(OSM_POI_FILE)
    osm_pois = annotate_pois_by_routes(pois, route_grids_by_file, ref_lat) if pois else []
    all_bus_stops = [poi for poi in pois if poi["kind"] == "bus_stop"]
    traffic_signals = [poi for poi in pois if poi["kind"] == "traffic_signal"]
    stop_evidence = attach_stop_evidence(osm_pois, stop_events, all_bus_stops, traffic_signals, ref_lat) if pois else {
        "stations_with_evidence": 0,
        "confirmed_stations": 0,
        "candidate_only_stations": 0,
    }
    output_road_segments = build_road_segments_output(segments_by_id, matches_by_file, osrm_segments)
    osm_counts = {
        "bus_stop": sum(1 for p in osm_pois if p["kind"] == "bus_stop"),
        "confirmed_stop_bus_stop": sum(1 for p in osm_pois if p["kind"] == "bus_stop" and p.get("stop_files")),
        "candidate_stop_bus_stop": sum(1 for p in osm_pois if p["kind"] == "bus_stop" and p.get("candidate_stop_files")),
        "traffic_signal": sum(1 for p in osm_pois if p["kind"] == "traffic_signal"),
    }

    output = {
        "summary": {
            "total_raw_points": total_raw_points,
            "total_compressed_points": total_compressed_points,
            "total_merged_repeats": total_merged_repeats,
            "bbox": bbox,
            "poi_source": "OpenStreetMap Overpass API",
            "road_source": "OpenStreetMap Overpass API",
            "road_match_max_meters": ROAD_MATCH_MAX_METERS,
            "route_snap_meters": ROUTE_SNAP_METERS,
            "stop_speed_kmh": STOP_SPEED_KMH,
            "stop_min_seconds": STOP_MIN_SECONDS,
            "stop_max_seconds": STOP_MAX_SECONDS,
            "stop_to_bus_stop_meters": STOP_TO_BUS_STOP_METERS,
            "confirmed_stop_min_events": CONFIRMED_STOP_MIN_EVENTS,
            "confirmed_stop_min_days": CONFIRMED_STOP_MIN_DAYS,
            "traffic_signal_filter_meters": TRAFFIC_SIGNAL_FILTER_METERS,
            "stop_events": len(stop_events),
            "stop_evidence": stop_evidence,
            "continuity_fill_by_file": continuity_fill_by_file,
            "graph_connect_by_file": graph_connect_by_file,
            "way_gap_fill_max_segments": WAY_GAP_FILL_MAX_SEGMENTS,
            "way_gap_fill_max_meters": WAY_GAP_FILL_MAX_METERS,
            "road_connect_max_aerial_meters": ROAD_CONNECT_MAX_AERIAL_METERS,
            "road_connect_max_path_meters": ROAD_CONNECT_MAX_PATH_METERS,
            "osrm_summary_by_file": osrm_summary_by_file,
            "compression": "Only consecutive identical latitude/longitude records are merged. The main map draws matched road geometry instead of raw GPS polylines.",
        },
        "routes": routes,
        "road_segments": output_road_segments,
        "charging_locations": charging_locations,
        "osm_pois": osm_pois,
        "osm_counts": osm_counts,
    }

    with DATA_JSON.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))
    HTML_FILE.write_text(build_html(DATA_JSON.name), encoding="utf-8")
    print(f"Wrote {DATA_JSON}")
    print(f"Wrote {HTML_FILE}")
    print(f"Routes: {len(routes)}, raw points: {total_raw_points}")
    print(f"Compressed route points: {total_compressed_points}, merged repeats: {total_merged_repeats}")
    print(f"Road segments loaded: {len(road_segments)}, drawn: {len(output_road_segments)}")
    for route in routes:
        match = route["road_match"]
        print(
            f"{route['file']}: local match {match['match_rate']:.1%}, "
            f"refined {match['refined_match_rate']:.1%}, "
            f"low confidence {match['low_confidence_segments']}, "
            f"OSRM {match['osrm_refined_segments']} ok/{match['osrm_failed_segments']} failed"
        )
    print(f"Charging clusters: {len(charging_locations)}")
    print(f"Stop events: {len(stop_events)}, stop evidence: {stop_evidence}")
    print(f"OSM POIs near road-matched routes: {osm_counts}")


if __name__ == "__main__":
    main()
