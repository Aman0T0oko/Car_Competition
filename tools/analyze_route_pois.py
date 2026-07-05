#!/usr/bin/env python3
import csv
import json
import math
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "浙江杭州"
OUTPUT_DIR = ROOT / "outputs"
OSM_POI_FILE = OUTPUT_DIR / "hangzhou_osm_poi.json"
CORRECTION_JOB_DIR = OUTPUT_DIR / "production" / "correction_jobs"
POI_GEOJSON = OUTPUT_DIR / "poi_analysis.geojson"
POI_REPORT_JSON = OUTPUT_DIR / "poi_analysis_report.json"
POI_REPORT_CSV = OUTPUT_DIR / "poi_analysis_report.csv"
POI_MAP_HTML = OUTPUT_DIR / "poi_analysis_map.html"
COMBINED_MAP_HTML = OUTPUT_DIR / "route_poi_dashboard.html"

LON_FIELD = "经度"
LAT_FIELD = "纬度"
TIME_FIELD = "数据时间"
SPEED_FIELD = "车速 km/h"
CHARGE_FIELD = "充电状态"
SOC_FIELD = "SOC"
MILEAGE_FIELD = "累计里程 km"

STOP_SPEED_KMH = 1.0
STOP_MIN_SECONDS = 20
STOP_MAX_SECONDS = 15 * 60
STOP_CLUSTER_METERS = 80
CHARGE_CLUSTER_METERS = 160
STOP_TO_BUS_STOP_METERS = 55
TRAFFIC_SIGNAL_FILTER_METERS = 35
ROUTE_TO_SIGNAL_METERS = 70
ROUTE_GRID_METERS = 120
CONFIRMED_STOP_MIN_EVENTS = 3
CONFIRMED_STOP_MIN_DAYS = 2


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


def build_index(items, ref_lat, cell_m):
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
    cells = int(math.ceil(max_m / cell_m)) + 1
    best = (float("inf"), None)
    for dx in range(-cells, cells + 1):
        for dy in range(-cells, cells + 1):
            for item_x, item_y, item in grid.get((ix + dx, iy + dy), []):
                dist = math.hypot(x - item_x, y - item_y)
                if dist < best[0]:
                    best = (dist, item)
    if best[1] is None or best[0] > max_m:
        return None, None
    return best[1], best[0]


def read_csv_evidence(path):
    first = None
    last = None
    stop_events = []
    charging_points = []
    current_stop = None
    valid_points = 0

    def finish_stop():
        nonlocal current_stop
        if not current_stop:
            return
        start_dt = current_stop["start_dt"]
        end_dt = current_stop["end_dt"]
        duration = int((end_dt - start_dt).total_seconds()) if start_dt and end_dt else 0
        if STOP_MIN_SECONDS <= duration <= STOP_MAX_SECONDS and current_stop["count"] >= 2:
            stop_events.append({
                "file": path.name,
                "lat": round(current_stop["lat_sum"] / current_stop["count"], 6),
                "lon": round(current_stop["lon_sum"] / current_stop["count"], 6),
                "start_time": current_stop["start_time"],
                "end_time": current_stop["end_time"],
                "duration_s": duration,
                "count": current_stop["count"],
            })
        current_stop = None

    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lon = parse_float(row.get(LON_FIELD))
            lat = parse_float(row.get(LAT_FIELD))
            timestamp = parse_time(row.get(TIME_FIELD))
            if not valid_coord(lon, lat) or timestamp is None:
                continue
            speed = parse_float(row.get(SPEED_FIELD), 0) or 0
            charge_state = str(row.get(CHARGE_FIELD, "")).strip()
            item = {
                "file": path.name,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "time": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "speed": speed,
                "charge": charge_state,
                "soc": str(row.get(SOC_FIELD, "")).strip(),
                "mileage": str(row.get(MILEAGE_FIELD, "")).strip(),
            }
            valid_points += 1
            if first is None:
                first = dict(item)
            last = dict(item)

            if charge_state in {"1", "4"}:
                charging_points.append(item)

            is_stopped = speed <= STOP_SPEED_KMH and charge_state not in {"1", "4"}
            if not is_stopped:
                finish_stop()
                continue

            if current_stop:
                gap_s = (timestamp - current_stop["end_dt"]).total_seconds()
                center = (
                    current_stop["lat_sum"] / current_stop["count"],
                    current_stop["lon_sum"] / current_stop["count"],
                )
                if gap_s > 90 or haversine_m((lat, lon), center) > STOP_CLUSTER_METERS:
                    finish_stop()
            if not current_stop:
                current_stop = {
                    "start_dt": timestamp,
                    "end_dt": timestamp,
                    "start_time": item["time"],
                    "end_time": item["time"],
                    "lat_sum": lat,
                    "lon_sum": lon,
                    "count": 1,
                }
            else:
                current_stop["end_dt"] = timestamp
                current_stop["end_time"] = item["time"]
                current_stop["lat_sum"] += lat
                current_stop["lon_sum"] += lon
                current_stop["count"] += 1
    finish_stop()
    return {
        "file": path.name,
        "valid_points": valid_points,
        "start": first,
        "end": last,
        "stop_events": stop_events,
        "charging_points": charging_points,
    }


def load_osm_pois():
    if not OSM_POI_FILE.exists():
        return [], []
    data = json.loads(OSM_POI_FILE.read_text(encoding="utf-8"))
    bus_stops = []
    traffic_signals = []
    for element in data.get("elements", []):
        tags = element.get("tags", {})
        lon = element.get("lon")
        lat = element.get("lat")
        if not valid_coord(lon, lat):
            continue
        highway = tags.get("highway", "")
        public_transport = tags.get("public_transport", "")
        item = {
            "id": element.get("id"),
            "lat": round(lat, 7),
            "lon": round(lon, 7),
            "name": tags.get("name") or tags.get("local_ref") or "",
            "tags": tags,
        }
        if highway == "traffic_signals":
            traffic_signals.append(item)
        elif highway == "bus_stop" or public_transport in {"platform", "station"}:
            bus_stops.append(item)
    return bus_stops, traffic_signals


def cluster_charging(points):
    clusters = []
    for point in points:
        chosen = None
        latlon = (point["lat"], point["lon"])
        for cluster in clusters:
            if haversine_m(latlon, (cluster["lat"], cluster["lon"])) <= CHARGE_CLUSTER_METERS:
                chosen = cluster
                break
        if chosen is None:
            chosen = {
                "id": f"charge-{len(clusters) + 1}",
                "lat": point["lat"],
                "lon": point["lon"],
                "count": 0,
                "files": set(),
                "charge_states": {},
                "first_time": point["time"],
                "last_time": point["time"],
            }
            clusters.append(chosen)
        chosen["count"] += 1
        chosen["lat"] = round((chosen["lat"] * (chosen["count"] - 1) + point["lat"]) / chosen["count"], 6)
        chosen["lon"] = round((chosen["lon"] * (chosen["count"] - 1) + point["lon"]) / chosen["count"], 6)
        chosen["files"].add(point["file"])
        chosen["charge_states"][point["charge"]] = chosen["charge_states"].get(point["charge"], 0) + 1
        chosen["first_time"] = min(chosen["first_time"], point["time"])
        chosen["last_time"] = max(chosen["last_time"], point["time"])
    normalized = []
    for index, cluster in enumerate(sorted(clusters, key=lambda item: item["count"], reverse=True), start=1):
        item = dict(cluster)
        item["id"] = f"charge-{index}"
        item["files"] = sorted(item["files"])
        normalized.append(item)
    return normalized


def attach_stop_evidence(stop_events, bus_stops, traffic_signals, ref_lat):
    bus_stop_index = build_index(bus_stops, ref_lat, STOP_TO_BUS_STOP_METERS)
    signal_index = build_index(traffic_signals, ref_lat, TRAFFIC_SIGNAL_FILTER_METERS)
    evidence = {}
    unmatched = []
    for event in stop_events:
        station, dist = nearest_indexed_item(
            (event["lat"], event["lon"]),
            bus_stop_index,
            ref_lat,
            STOP_TO_BUS_STOP_METERS,
            STOP_TO_BUS_STOP_METERS,
        )
        if not station:
            unmatched.append(event)
            continue
        signal, signal_dist = nearest_indexed_item(
            (event["lat"], event["lon"]),
            signal_index,
            ref_lat,
            TRAFFIC_SIGNAL_FILTER_METERS,
            TRAFFIC_SIGNAL_FILTER_METERS,
        )
        station_key = str(station["id"])
        stats = evidence.setdefault(station_key, {
            "station": station,
            "files": {},
            "total_events": 0,
            "total_dwell_s": 0,
            "nearest_stop_event_m": round(dist, 1),
            "near_traffic_signal_events": 0,
            "nearest_traffic_signal_m": None,
        })
        file_stats = stats["files"].setdefault(event["file"], {
            "events": 0,
            "dates": set(),
            "total_dwell_s": 0,
            "max_dwell_s": 0,
            "first_time": event["start_time"],
            "last_time": event["end_time"],
        })
        file_stats["events"] += 1
        file_stats["dates"].add(event["start_time"][:10])
        file_stats["total_dwell_s"] += event["duration_s"]
        file_stats["max_dwell_s"] = max(file_stats["max_dwell_s"], event["duration_s"])
        file_stats["first_time"] = min(file_stats["first_time"], event["start_time"])
        file_stats["last_time"] = max(file_stats["last_time"], event["end_time"])
        stats["total_events"] += 1
        stats["total_dwell_s"] += event["duration_s"]
        stats["nearest_stop_event_m"] = min(stats["nearest_stop_event_m"], round(dist, 1))
        if signal:
            rounded = round(signal_dist, 1)
            stats["near_traffic_signal_events"] += 1
            if stats["nearest_traffic_signal_m"] is None:
                stats["nearest_traffic_signal_m"] = rounded
            else:
                stats["nearest_traffic_signal_m"] = min(stats["nearest_traffic_signal_m"], rounded)

    stations = []
    for station_key, stats in evidence.items():
        files = {}
        confirmed_files = []
        candidate_files = []
        for file_name, file_stats in sorted(stats["files"].items()):
            service_days = len(file_stats["dates"])
            status = "confirmed"
            if file_stats["events"] < CONFIRMED_STOP_MIN_EVENTS or service_days < CONFIRMED_STOP_MIN_DAYS:
                status = "candidate"
            if status == "confirmed":
                confirmed_files.append(file_name)
            else:
                candidate_files.append(file_name)
            files[file_name] = {
                "events": file_stats["events"],
                "service_days": service_days,
                "total_dwell_s": file_stats["total_dwell_s"],
                "max_dwell_s": file_stats["max_dwell_s"],
                "first_time": file_stats["first_time"],
                "last_time": file_stats["last_time"],
                "status": status,
            }
        station = dict(stats["station"])
        station.update({
            "kind": "bus_stop",
            "status": "confirmed" if confirmed_files else "candidate",
            "confirmed_files": confirmed_files,
            "candidate_files": candidate_files,
            "files": sorted(set(confirmed_files + candidate_files)),
            "stop_stats": files,
            "total_events": stats["total_events"],
            "total_dwell_s": stats["total_dwell_s"],
            "nearest_stop_event_m": stats["nearest_stop_event_m"],
            "near_traffic_signal_events": stats["near_traffic_signal_events"],
            "nearest_traffic_signal_m": stats["nearest_traffic_signal_m"],
        })
        stations.append(station)
    stations.sort(key=lambda item: (item["status"] != "confirmed", -(item["total_events"]), item.get("name") or ""))
    return stations, unmatched


def load_route_points_from_jobs():
    points = []
    if not CORRECTION_JOB_DIR.exists():
        return points
    for route_dir in sorted(CORRECTION_JOB_DIR.iterdir()):
        if not route_dir.is_dir():
            continue
        file_name = f"{route_dir.name}.csv"
        for job_path in sorted(route_dir.glob("*.json")):
            data = json.loads(job_path.read_text(encoding="utf-8"))
            for point in data.get("points", []):
                lat = parse_float(point.get("lat"))
                lon = parse_float(point.get("lon"))
                if valid_coord(lon, lat):
                    points.append({"lat": lat, "lon": lon, "file": file_name})
    return points


def traffic_signals_near_routes(traffic_signals, route_points, ref_lat):
    if not route_points:
        return []
    route_index = build_index(route_points, ref_lat, ROUTE_GRID_METERS)
    selected = []
    for signal in traffic_signals:
        nearest, dist = nearest_indexed_item(
            (signal["lat"], signal["lon"]),
            route_index,
            ref_lat,
            ROUTE_GRID_METERS,
            ROUTE_TO_SIGNAL_METERS,
        )
        if not nearest:
            continue
        files = set()
        x, y = project(signal["lon"], signal["lat"], ref_lat)
        ix = int(x // ROUTE_GRID_METERS)
        iy = int(y // ROUTE_GRID_METERS)
        cells = int(math.ceil(ROUTE_TO_SIGNAL_METERS / ROUTE_GRID_METERS)) + 1
        for dx in range(-cells, cells + 1):
            for dy in range(-cells, cells + 1):
                for item_x, item_y, item in route_index.get((ix + dx, iy + dy), []):
                    if math.hypot(x - item_x, y - item_y) <= ROUTE_TO_SIGNAL_METERS:
                        files.add(item["file"])
        item = dict(signal)
        item.update({
            "kind": "traffic_signal",
            "files": sorted(files),
            "distance_to_route_m": round(dist, 1),
        })
        selected.append(item)
    selected.sort(key=lambda item: (item.get("files") or [], item["distance_to_route_m"]))
    return selected


def feature(point, properties):
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [point["lon"], point["lat"]],
        },
        "properties": properties,
    }


def build_geojson(endpoints, charging_clusters, stations, traffic_signals):
    features = []
    for item in endpoints:
        props = {
            "kind": item["kind"],
            "file": item["file"],
            "time": item["time"],
            "soc": item.get("soc"),
            "mileage": item.get("mileage"),
            "name": f"{item['file']} {item['kind']}",
        }
        features.append(feature(item, props))
    for item in charging_clusters:
        props = {
            "kind": "charging_location",
            "name": item["id"],
            "files": ",".join(item["files"]),
            "count": item["count"],
            "first_time": item["first_time"],
            "last_time": item["last_time"],
            "charge_states": json.dumps(item["charge_states"], ensure_ascii=False),
        }
        features.append(feature(item, props))
    for item in stations:
        station_times = []
        for stats in item.get("stop_stats", {}).values():
            if stats.get("first_time"):
                station_times.append(stats["first_time"])
            if stats.get("last_time"):
                station_times.append(stats["last_time"])
        props = {
            "kind": "bus_stop",
            "status": item["status"],
            "name": item.get("name") or "未命名站点",
            "files": ",".join(item["files"]),
            "confirmed_files": ",".join(item["confirmed_files"]),
            "candidate_files": ",".join(item["candidate_files"]),
            "total_events": item["total_events"],
            "total_dwell_s": item["total_dwell_s"],
            "nearest_stop_event_m": item["nearest_stop_event_m"],
            "near_traffic_signal_events": item["near_traffic_signal_events"],
            "nearest_traffic_signal_m": item["nearest_traffic_signal_m"],
            "first_time": min(station_times) if station_times else "",
            "last_time": max(station_times) if station_times else "",
        }
        features.append(feature(item, props))
    for item in traffic_signals:
        props = {
            "kind": "traffic_signal",
            "name": item.get("name") or "交通信号灯",
            "files": ",".join(item["files"]),
            "distance_to_route_m": item["distance_to_route_m"],
        }
        features.append(feature(item, props))
    return {
        "type": "FeatureCollection",
        "features": features,
    }


def write_report_csv(endpoints, charging_clusters, stations, traffic_signals):
    rows = []
    for item in endpoints:
        rows.append({
            "kind": item["kind"],
            "name": f"{item['file']} {item['kind']}",
            "status": "",
            "files": item["file"],
            "lat": item["lat"],
            "lon": item["lon"],
            "count": "",
            "events": "",
            "details": item["time"],
        })
    for item in charging_clusters:
        rows.append({
            "kind": "charging_location",
            "name": item["id"],
            "status": "",
            "files": ",".join(item["files"]),
            "lat": item["lat"],
            "lon": item["lon"],
            "count": item["count"],
            "events": "",
            "details": f"{item['first_time']} 至 {item['last_time']}",
        })
    for item in stations:
        rows.append({
            "kind": "bus_stop",
            "name": item.get("name") or "未命名站点",
            "status": item["status"],
            "files": ",".join(item["files"]),
            "lat": item["lat"],
            "lon": item["lon"],
            "count": "",
            "events": item["total_events"],
            "details": f"停靠{item['total_events']}次，累计{item['total_dwell_s']}秒",
        })
    for item in traffic_signals:
        rows.append({
            "kind": "traffic_signal",
            "name": item.get("name") or "交通信号灯",
            "status": "",
            "files": ",".join(item["files"]),
            "lat": item["lat"],
            "lon": item["lon"],
            "count": "",
            "events": "",
            "details": f"距路线{item['distance_to_route_m']}米",
        })
    fieldnames = ["kind", "name", "status", "files", "lat", "lon", "count", "events", "details"]
    with POI_REPORT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_map_html():
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>公交点位分析</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map { height: 100%; margin: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .panel {
      position: absolute;
      right: 12px;
      bottom: 20px;
      z-index: 900;
      width: min(380px, calc(100vw - 24px));
      background: rgba(255,255,255,.94);
      border: 1px solid #d4dae5;
      border-radius: 8px;
      box-shadow: 0 10px 28px rgba(31,41,55,.18);
      padding: 12px 14px;
      color: #172033;
      font-size: 13px;
      line-height: 1.55;
    }
    .panel strong { display: block; font-size: 15px; margin-bottom: 7px; }
    .row { display: flex; justify-content: space-between; gap: 14px; }
    .leaflet-popup-content { font-size: 13px; line-height: 1.45; }
  </style>
</head>
<body>
  <div id="map"></div>
  <aside class="panel">
    <strong>公交点位分析</strong>
    <div id="stats"></div>
  </aside>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const map = L.map("map", { preferCanvas: true });
    const base = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }).addTo(map);
    const styles = {
      start: ["#0f766e", 8],
      end: ["#991b1b", 8],
      charging_location: ["#f59e0b", 7],
      bus_stop: ["#2563eb", 5],
      traffic_signal: ["#dc2626", 4]
    };
    const labels = {
      start: "起点",
      end: "终点",
      charging_location: "充电位置",
      bus_stop: "站点",
      traffic_signal: "红绿灯"
    };
    const layers = {};
    Object.keys(labels).forEach(kind => {
      layers[labels[kind]] = L.layerGroup().addTo(map);
    });
    L.control.layers({"OpenStreetMap": base}, layers, { collapsed: false }).addTo(map);

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
      }[ch]));
    }

    function popup(props) {
      return `<strong>${escapeHtml(props.name || props.kind)}</strong><br>` +
        Object.entries(props)
          .filter(([key]) => !["name"].includes(key))
          .map(([key, value]) => `${escapeHtml(key)}：${escapeHtml(value)}`)
          .join("<br>");
    }

    fetch("poi_analysis.geojson").then(r => r.json()).then(data => {
      const counts = {};
      const bounds = [];
      data.features.forEach(feature => {
        const props = feature.properties || {};
        const kind = props.kind;
        const [color, radius] = styles[kind] || ["#334155", 5];
        const [lon, lat] = feature.geometry.coordinates;
        counts[kind] = (counts[kind] || 0) + 1;
        bounds.push([lat, lon]);
        const marker = L.circleMarker([lat, lon], {
          radius,
          color,
          weight: 2,
          fillColor: color,
          fillOpacity: kind === "traffic_signal" ? 0.65 : 0.85
        }).bindPopup(popup(props));
        marker.addTo(layers[labels[kind]] || layers["站点"]);
      });
      document.getElementById("stats").innerHTML = Object.keys(labels).map(kind =>
        `<div class="row"><span>${labels[kind]}</span><span>${(counts[kind] || 0).toLocaleString()}</span></div>`
      ).join("");
      if (bounds.length) map.fitBounds(bounds, { padding: [24, 24] });
      else map.setView([30.27, 120.18], 12);
    });
  </script>
</body>
</html>
"""


def build_combined_map_html():
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>路线与点位综合图</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map { height: 100%; margin: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .panel {
      position: absolute;
      right: 12px;
      bottom: 20px;
      z-index: 900;
      width: min(390px, calc(100vw - 24px));
      max-height: min(56vh, 520px);
      overflow: auto;
      background: rgba(255,255,255,.94);
      border: 1px solid #d4dae5;
      border-radius: 8px;
      box-shadow: 0 10px 28px rgba(31,41,55,.18);
      padding: 12px 14px;
      color: #172033;
      font-size: 13px;
      line-height: 1.55;
    }
    .panel strong { display: block; font-size: 15px; margin-bottom: 7px; }
    .row { display: flex; justify-content: space-between; gap: 14px; }
    .section { margin-top: 10px; padding-top: 8px; border-top: 1px solid #e3e8f0; }
    .filters { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0 10px; }
    .field { display: grid; gap: 3px; }
    .field label { font-size: 12px; color: #526071; }
    select {
      width: 100%;
      min-height: 32px;
      border: 1px solid #cfd6e3;
      border-radius: 6px;
      background: #fff;
      color: #172033;
      font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      padding: 4px 7px;
    }
    .ui-toggle {
      position: absolute;
      left: 12px;
      bottom: 20px;
      z-index: 1100;
      min-width: 76px;
      min-height: 34px;
      border: 1px solid #cfd6e3;
      border-radius: 8px;
      background: rgba(255,255,255,.96);
      box-shadow: 0 8px 22px rgba(31,41,55,.16);
      color: #152033;
      cursor: pointer;
      font: 600 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    body.ui-hidden .panel,
    body.ui-hidden .leaflet-control-layers { display: none; }
    .leaflet-popup-content { font-size: 13px; line-height: 1.45; }
    @media (max-width: 680px) {
      .panel { left: 10px; right: 10px; bottom: 62px; width: auto; max-height: 42vh; }
      .ui-toggle { left: 10px; bottom: 18px; }
    }
  </style>
</head>
<body>
  <div id="map"></div>
  <button class="ui-toggle" id="uiToggle" type="button" aria-pressed="false">隐藏 UI</button>
  <aside class="panel">
    <strong>路线与点位综合图</strong>
    <div class="filters">
      <div class="field">
        <label for="startDateSelect">开始日期</label>
        <select id="startDateSelect"></select>
      </div>
      <div class="field">
        <label for="endDateSelect">结束日期</label>
        <select id="endDateSelect"></select>
      </div>
    </div>
    <div class="row"><span>A/B 可用路线</span><span id="acceptedCount">0</span></div>
    <div class="row"><span>C/D 复核片段</span><span id="reviewCount">0</span></div>
    <div id="routeBreakdown"></div>
    <div class="section" id="poiStats"></div>
  </aside>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const map = L.map("map", { preferCanvas: true });
    const base = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }).addTo(map);
    const routeColors = {
      "01.csv": "#2563eb",
      "02.csv": "#16a34a",
      "03.csv": "#dc2626",
      "04.csv": "#7c3aed",
      "05.csv": "#ea580c"
    };
    const poiStyles = {
      start: ["#0f766e", 8, "起点"],
      end: ["#991b1b", 8, "终点"],
      charging_location: ["#f59e0b", 7, "充电位置"],
      bus_stop: ["#2563eb", 5, "站点"],
      traffic_signal: ["#dc2626", 4, "红绿灯"]
    };
    const overlays = {};
    const visibleLayers = [];
    const routeAcceptedLayers = {};
    const routeReviewLayers = {};
    const poiLayers = {};
    let allAcceptedLines = [];
    let allReviewLines = [];
    let allPoiFeatures = [];

    document.getElementById("uiToggle").addEventListener("click", event => {
      const hidden = document.body.classList.toggle("ui-hidden");
      event.currentTarget.textContent = hidden ? "显示 UI" : "隐藏 UI";
      event.currentTarget.setAttribute("aria-pressed", String(hidden));
    });

    function colorFor(file) {
      return routeColors[file] || "#334155";
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
      }[ch]));
    }

    function routePopup(feature, layer) {
      const p = feature.properties || {};
      layer.bindPopup(`
        <strong>${escapeHtml(p.file)} · ${escapeHtml(p.job_id)}</strong><br>
        等级：${escapeHtml(p.grade)}，分数：${escapeHtml(p.score)}<br>
        状态：${escapeHtml(p.status)}<br>
        原因：${escapeHtml(p.reasons || "无")}<br>
        时间：${escapeHtml(p.start_time)} 至 ${escapeHtml(p.end_time)}<br>
        里程：${escapeHtml(p.corrected_distance_m || p.raw_distance_m || "")} m
      `);
    }

    function poiPopup(props) {
      return `<strong>${escapeHtml(props.name || props.kind)}</strong><br>` +
        Object.entries(props)
          .filter(([key]) => !["name"].includes(key))
          .map(([key, value]) => `${escapeHtml(key)}：${escapeHtml(value)}`)
          .join("<br>");
    }

    function addOverlay(name, layer, visible = true) {
      overlays[name] = layer;
      if (visible) layer.addTo(map);
      visibleLayers.push(layer);
    }

    function dateOnly(value) {
      const text = String(value || "");
      return /^\\d{4}-\\d{2}-\\d{2}/.test(text) ? text.slice(0, 10) : "";
    }

    function featureDateRange(feature) {
      const p = feature.properties || {};
      const start = dateOnly(p.start_time || p.time || p.first_time);
      const end = dateOnly(p.end_time || p.time || p.last_time || p.start_time || p.first_time);
      return [start, end || start];
    }

    function dayList(start, end) {
      const days = [];
      const [sy, sm, sd] = start.split("-").map(Number);
      const [ey, em, ed] = end.split("-").map(Number);
      const current = new Date(Date.UTC(sy, sm - 1, sd));
      const last = new Date(Date.UTC(ey, em - 1, ed));
      while (current <= last) {
        days.push(current.toISOString().slice(0, 10));
        current.setUTCDate(current.getUTCDate() + 1);
      }
      return days;
    }

    function inDateRange(feature, startDate, endDate) {
      const [start, end] = featureDateRange(feature);
      if (!start && !end) return true;
      return (end || start) >= startDate && (start || end) <= endDate;
    }

    function populateDateControls(features) {
      const dates = [];
      features.forEach(feature => {
        const [start, end] = featureDateRange(feature);
        if (start) dates.push(start);
        if (end) dates.push(end);
      });
      dates.sort();
      const minDate = dates[0];
      const maxDate = dates[dates.length - 1];
      const days = minDate && maxDate ? dayList(minDate, maxDate) : [];
      const startSelect = document.getElementById("startDateSelect");
      const endSelect = document.getElementById("endDateSelect");
      startSelect.innerHTML = days.map(day => `<option value="${day}">${day}</option>`).join("");
      endSelect.innerHTML = days.map(day => `<option value="${day}">${day}</option>`).join("");
      if (days.length) {
        startSelect.value = days[0];
        endSelect.value = days[days.length - 1];
      }
      const onChange = () => {
        if (startSelect.value > endSelect.value) endSelect.value = startSelect.value;
        renderFiltered();
      };
      startSelect.addEventListener("change", onChange);
      endSelect.addEventListener("change", onChange);
    }

    function addRouteFeature(layer, feature, color, opacity) {
      L.geoJSON(feature, {
        style: () => ({ color, weight: 2, opacity }),
        onEachFeature: routePopup
      }).eachLayer(item => layer.addLayer(item));
    }

    function addPoiFeature(layer, feature, color, radius) {
      const props = feature.properties || {};
      const [lon, lat] = feature.geometry.coordinates;
      L.circleMarker([lat, lon], {
        radius,
        color,
        weight: 2,
        fillColor: color,
        fillOpacity: props.kind === "traffic_signal" ? 0.65 : 0.86
      }).bindPopup(poiPopup(props)).addTo(layer);
    }

    function renderFiltered() {
      const startDate = document.getElementById("startDateSelect").value;
      const endDate = document.getElementById("endDateSelect").value;
      const files = Array.from(new Set(
        allAcceptedLines.concat(allReviewLines).map(f => f.properties.file)
      )).sort();
      const routeBreakdown = [];
      let acceptedCount = 0;
      let reviewCount = 0;

      Object.values(routeAcceptedLayers).forEach(layer => layer.clearLayers());
      Object.values(routeReviewLayers).forEach(layer => layer.clearLayers());
      Object.values(poiLayers).forEach(layer => layer.clearLayers());

      files.forEach(file => {
        const color = colorFor(file);
        const acceptedForFile = allAcceptedLines.filter(feature => feature.properties.file === file);
        const reviewForFile = allReviewLines.filter(feature => feature.properties.file === file);
        const acceptedFiltered = acceptedForFile.filter(feature => inDateRange(feature, startDate, endDate));
        const reviewFiltered = reviewForFile.filter(feature => inDateRange(feature, startDate, endDate));
        acceptedFiltered.forEach(feature => addRouteFeature(routeAcceptedLayers[file], feature, color, 0.9));
        reviewFiltered.forEach(feature => addRouteFeature(routeReviewLayers[file], feature, color, 0.42));
        acceptedCount += acceptedFiltered.length;
        reviewCount += reviewFiltered.length;
        routeBreakdown.push(`<div class="row"><span><span style="display:inline-block;width:22px;height:4px;border-radius:3px;background:${color};vertical-align:2px;margin-right:7px"></span>${file}</span><span>${acceptedFiltered.length}/${reviewFiltered.length}</span></div>`);
      });

      const poiCounts = {};
      allPoiFeatures.forEach(feature => {
        const props = feature.properties || {};
        const kind = props.kind;
        if (kind !== "traffic_signal" && !inDateRange(feature, startDate, endDate)) return;
        const [color, radius, label] = poiStyles[kind] || ["#334155", 5, "其他点位"];
        poiCounts[kind] = (poiCounts[kind] || 0) + 1;
        addPoiFeature(poiLayers[label], feature, color, radius);
      });

      document.getElementById("acceptedCount").textContent = acceptedCount.toLocaleString();
      document.getElementById("reviewCount").textContent = reviewCount.toLocaleString();
      document.getElementById("routeBreakdown").innerHTML = routeBreakdown.join("");
      document.getElementById("poiStats").innerHTML = Object.entries(poiStyles).map(([kind, [, , label]]) =>
        `<div class="row"><span>${label}</span><span>${(poiCounts[kind] || 0).toLocaleString()}</span></div>`
      ).join("");
    }

    function featureBounds(features) {
      const bounds = L.latLngBounds([]);
      features.forEach(feature => {
        const geometry = feature.geometry || {};
        if (geometry.type === "Point") {
          const [lon, lat] = geometry.coordinates;
          bounds.extend([lat, lon]);
        }
        if (geometry.type === "LineString") {
          geometry.coordinates.forEach(([lon, lat]) => bounds.extend([lat, lon]));
        }
      });
      return bounds;
    }

    Promise.all([
      fetch("corrected_routes.geojson").then(r => r.json()),
      fetch("review_segments.geojson").then(r => r.json()),
      fetch("poi_analysis.geojson").then(r => r.json())
    ]).then(([accepted, review, pois]) => {
      allAcceptedLines = accepted.features.filter(f => f.geometry.type === "LineString");
      allReviewLines = review.features.filter(f => f.geometry.type === "LineString");
      allPoiFeatures = pois.features.filter(f => f.geometry.type === "Point");
      const files = Array.from(new Set(
        allAcceptedLines.concat(allReviewLines).map(f => f.properties.file)
      )).sort();

      files.forEach(file => {
        routeAcceptedLayers[file] = L.layerGroup();
        routeReviewLayers[file] = L.layerGroup();
        addOverlay(`${file} A/B 可用`, routeAcceptedLayers[file], true);
        addOverlay(`${file} C/D 复核`, routeReviewLayers[file], true);
      });

      Object.entries(poiStyles).forEach(([kind, [, , label]]) => {
        poiLayers[label] = L.layerGroup();
        addOverlay(label, poiLayers[label], true);
      });

      L.control.layers({"OpenStreetMap": base}, overlays, { collapsed: false }).addTo(map);
      populateDateControls(allAcceptedLines.concat(allReviewLines, allPoiFeatures));
      renderFiltered();
      const bounds = featureBounds(allAcceptedLines.concat(allReviewLines, allPoiFeatures));
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24] });
      else map.setView([30.27, 120.18], 12);
    });
  </script>
</body>
</html>
"""


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    payloads = []
    all_charging = []
    all_stops = []
    endpoints = []
    bbox = [999, 999, -999, -999]
    for path in sorted(DATA_DIR.glob("*.csv")):
        print(f"Analyzing {path.name}", flush=True)
        payload = read_csv_evidence(path)
        payloads.append(payload)
        all_charging.extend(payload["charging_points"])
        all_stops.extend(payload["stop_events"])
        for kind in ("start", "end"):
            point = payload[kind]
            if point:
                item = dict(point)
                item["kind"] = kind
                endpoints.append(item)
        for point in [payload["start"], payload["end"]]:
            if point:
                bbox[0] = min(bbox[0], point["lon"])
                bbox[1] = min(bbox[1], point["lat"])
                bbox[2] = max(bbox[2], point["lon"])
                bbox[3] = max(bbox[3], point["lat"])

    ref_lat = (bbox[1] + bbox[3]) / 2 if bbox[0] < 900 else 30.27
    bus_stops, traffic_signals = load_osm_pois()
    charging_clusters = cluster_charging(all_charging)
    stations, unmatched_stop_events = attach_stop_evidence(all_stops, bus_stops, traffic_signals, ref_lat)
    route_points = load_route_points_from_jobs()
    route_signals = traffic_signals_near_routes(traffic_signals, route_points, ref_lat)
    geojson = build_geojson(endpoints, charging_clusters, stations, route_signals)
    POI_GEOJSON.write_text(json.dumps(geojson, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    write_report_csv(endpoints, charging_clusters, stations, route_signals)
    POI_MAP_HTML.write_text(build_map_html(), encoding="utf-8")
    COMBINED_MAP_HTML.write_text(build_combined_map_html(), encoding="utf-8")
    report = {
        "inputs": {
            "csv_files": [payload["file"] for payload in payloads],
            "osm_poi_file": str(OSM_POI_FILE.relative_to(ROOT)),
            "route_point_source": str(CORRECTION_JOB_DIR.relative_to(ROOT)),
        },
        "counts": {
            "endpoints": len(endpoints),
            "charging_locations": len(charging_clusters),
            "stop_events": len(all_stops),
            "matched_stop_events": len(all_stops) - len(unmatched_stop_events),
            "unmatched_stop_events": len(unmatched_stop_events),
            "stations_total": len(stations),
            "stations_confirmed": sum(1 for item in stations if item["status"] == "confirmed"),
            "stations_candidate": sum(1 for item in stations if item["status"] == "candidate"),
            "traffic_signals": len(route_signals),
        },
        "thresholds": {
            "stop_speed_kmh": STOP_SPEED_KMH,
            "stop_min_seconds": STOP_MIN_SECONDS,
            "stop_max_seconds": STOP_MAX_SECONDS,
            "charge_cluster_meters": CHARGE_CLUSTER_METERS,
            "stop_to_bus_stop_meters": STOP_TO_BUS_STOP_METERS,
            "route_to_signal_meters": ROUTE_TO_SIGNAL_METERS,
            "confirmed_stop_min_events": CONFIRMED_STOP_MIN_EVENTS,
            "confirmed_stop_min_days": CONFIRMED_STOP_MIN_DAYS,
        },
        "outputs": {
            "geojson": str(POI_GEOJSON.relative_to(ROOT)),
            "csv": str(POI_REPORT_CSV.relative_to(ROOT)),
            "html": str(POI_MAP_HTML.relative_to(ROOT)),
            "combined_html": str(COMBINED_MAP_HTML.relative_to(ROOT)),
        },
    }
    POI_REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
