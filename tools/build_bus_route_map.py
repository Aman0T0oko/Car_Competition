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
DATA_JSON = OUTPUT_DIR / "bus_route_map_data.json"
HTML_FILE = OUTPUT_DIR / "hangzhou_bus_route_map.html"

LON_FIELD = "经度"
LAT_FIELD = "纬度"
TIME_FIELD = "数据时间"
SPEED_FIELD = "车速 km/h"
CHARGE_FIELD = "充电状态"
SOC_FIELD = "SOC"
MILEAGE_FIELD = "累计里程 km"

ROUTE_SNAP_METERS = 95
CHARGE_CLUSTER_METERS = 160
GRID_SIZE_METERS = 120
STOP_SPEED_KMH = 1.0
STOP_MIN_SECONDS = 20
STOP_MAX_SECONDS = 15 * 60
STOP_CLUSTER_METERS = 80
STOP_TO_BUS_STOP_METERS = 95


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


def point_segment_distance_m(point, a, b, ref_lat):
    px, py = project(point[1], point[0], ref_lat)
    ax, ay = project(a[1], a[0], ref_lat)
    bx, by = project(b[1], b[0], ref_lat)
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def project(lon, lat, ref_lat):
    return (
        lon * 111320 * math.cos(math.radians(ref_lat)),
        lat * 110540,
    )


def min_distance_to_route(point, route_points, ref_lat):
    if not route_points:
        return float("inf")
    if len(route_points) == 1:
        return haversine_m(point, route_points[0])
    best = float("inf")
    # Route data is already sampled, so checking each segment is acceptable.
    for i in range(len(route_points) - 1):
        dist = point_segment_distance_m(point, route_points[i], route_points[i + 1], ref_lat)
        if dist < best:
            best = dist
            if best <= ROUTE_SNAP_METERS:
                return best
    return best


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


def encode_polyline6(points):
    result = []
    last_lat = 0
    last_lon = 0
    for point in points:
        lat = int(round(point["lat"] * 1_000_000))
        lon = int(round(point["lon"] * 1_000_000))
        result.append(encode_signed(lat - last_lat))
        result.append(encode_signed(lon - last_lon))
        last_lat = lat
        last_lon = lon
    return "".join(result)


def encode_signed(value):
    value = ~(value << 1) if value < 0 else value << 1
    chunks = []
    while value >= 0x20:
        chunks.append(chr((0x20 | (value & 0x1f)) + 63))
        value >>= 5
    chunks.append(chr(value + 63))
    return "".join(chunks)


def build_route_vertex_index(route_points, ref_lat):
    grid = {}
    for lat, lon in route_points:
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
            poi["stop_stats"] = {}
            selected.append(poi)
            seen.add(key)
    selected.sort(key=lambda p: (p["kind"], p.get("name") or "", p["distance_m"]))
    return selected


def attach_stop_evidence(osm_pois, stop_events, all_bus_stops, ref_lat):
    bus_stop_index = build_poi_index(all_bus_stops, ref_lat, STOP_TO_BUS_STOP_METERS)
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
        station_id = station["id"]
        by_file = evidence.setdefault(station_id, {})
        stats = by_file.setdefault(event["file"], {
            "events": 0,
            "total_dwell_s": 0,
            "max_dwell_s": 0,
            "first_time": event["start_time"],
            "last_time": event["end_time"],
            "nearest_stop_event_m": round(dist, 1),
        })
        stats["events"] += 1
        stats["total_dwell_s"] += event["duration_s"]
        stats["max_dwell_s"] = max(stats["max_dwell_s"], event["duration_s"])
        stats["first_time"] = min(stats["first_time"], event["start_time"])
        stats["last_time"] = max(stats["last_time"], event["end_time"])
        stats["nearest_stop_event_m"] = min(stats["nearest_stop_event_m"], round(dist, 1))

    poi_by_id = {poi["id"]: poi for poi in osm_pois if poi["kind"] == "bus_stop"}
    for station_id, by_file in evidence.items():
        poi = poi_by_id.get(station_id)
        if not poi:
            continue
        poi["stop_files"] = sorted(by_file)
        poi["stop_stats"] = dict(sorted(by_file.items()))
    return evidence


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
      width: min(360px, calc(100vw - 24px));
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
      width: 210px;
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
        width: min(210px, calc(100vw - 20px));
      }}
      .panel {{
        left: 10px;
        right: 10px;
        bottom: 18px;
        width: auto;
        max-height: 38vh;
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
      "行驶路径": routeLayers,
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

    function decodePolyline6(encoded, count) {{
      const coords = new Int32Array(count * 2);
      let index = 0;
      let lat = 0;
      let lon = 0;
      let out = 0;
      while (index < encoded.length && out < coords.length) {{
        let shift = 0;
        let result = 0;
        let byte = 0;
        do {{
          byte = encoded.charCodeAt(index++) - 63;
          result |= (byte & 0x1f) << shift;
          shift += 5;
        }} while (byte >= 0x20);
        lat += (result & 1) ? ~(result >> 1) : (result >> 1);

        shift = 0;
        result = 0;
        do {{
          byte = encoded.charCodeAt(index++) - 63;
          result |= (byte & 0x1f) << shift;
          shift += 5;
        }} while (byte >= 0x20);
        lon += (result & 1) ? ~(result >> 1) : (result >> 1);

        coords[out++] = lat;
        coords[out++] = lon;
      }}
      return coords;
    }}

    const CanvasRouteLayer = L.Layer.extend({{
      initialize(routes) {{
        this.routes = routes.map((route, index) => ({{
          file: route.file,
          color: colors[index % colors.length],
          coords: decodePolyline6(route.encoded_path, route.compressed_count),
          visible: true
        }}));
      }},
      setVisible(file, visible) {{
        const route = this.routes.find(item => item.file === file);
        if (!route) return;
        route.visible = visible;
        if (this.map) this.reset();
      }},
      onAdd(mapInstance) {{
        this.map = mapInstance;
        this.canvas = L.DomUtil.create("canvas", "leaflet-zoom-animated");
        this.ctx = this.canvas.getContext("2d");
        mapInstance.getPanes().overlayPane.appendChild(this.canvas);
        mapInstance.on("move zoom resize zoomend", this.reset, this);
        this.reset();
      }},
      onRemove(mapInstance) {{
        mapInstance.off("move zoom resize zoomend", this.reset, this);
        this.canvas.remove();
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
        const visibleRoutes = this.routes
          .filter(route => route.visible)
          .sort((a, b) => {{
            if (a.file === "01.csv") return 1;
            if (b.file === "01.csv") return -1;
            return 0;
          }});
        const drawRoute = (route, strokeStyle, lineWidth, alpha) => {{
          if (!route.visible) return;
          const coords = route.coords;
          if (coords.length < 4) return;
          ctx.beginPath();
          ctx.strokeStyle = strokeStyle;
          ctx.lineWidth = lineWidth;
          ctx.globalAlpha = alpha;
          for (let i = 0; i < coords.length; i += 2) {{
            const pt = this.map.latLngToContainerPoint([coords[i] / 1e6, coords[i + 1] / 1e6]);
            if (i === 0) ctx.moveTo(pt.x, pt.y);
            else ctx.lineTo(pt.x, pt.y);
          }}
          ctx.stroke();
        }};
        for (const route of visibleRoutes) {{
          drawRoute(route, "rgba(255, 255, 255, 0.92)", 7, 0.78);
          drawRoute(route, route.color, route.file === "01.csv" ? 4.8 : 3.6, route.file === "01.csv" ? 0.98 : 0.86);
        }}
        ctx.globalAlpha = 1;
      }}
    }});

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

    function formatFiles(files) {{
      return (files || []).map(file => `<code>${{escapeHtml(file)}}</code>`).join("、") || "无";
    }}

    function hasActiveFile(files) {{
      return (files || []).some(file => activeFiles.has(file));
    }}

    function addLinkedMarker(group, marker, files) {{
      const record = {{ group, marker, files: files || [] }};
      linkedMarkerRecords.push(record);
      if (hasActiveFile(record.files)) {{
        marker.addTo(group);
      }}
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
          <span><span class="line" style="background:${{colors[index % colors.length]}}"></span>${{escapeHtml(route.file)}}</span>
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
        const canvasRoutes = new CanvasRouteLayer(data.routes).addTo(routeLayers);
        data.routes.forEach((route, index) => {{
          const color = colors[index % colors.length];
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
          const hasStopEvidence = item.stop_files && item.stop_files.length;
          const statsHtml = hasStopEvidence
            ? Object.entries(item.stop_stats).map(([file, stats]) =>
                `<br><code>${{escapeHtml(file)}}</code>：停靠 ${{stats.events}} 次，累计 ${{formatDuration(stats.total_dwell_s)}}，最长 ${{formatDuration(stats.max_dwell_s)}}`
              ).join("")
            : "<br>停靠证据：未检测到达到阈值的停靠";
          const marker = circle(item.lat, item.lon, hasStopEvidence ? "#1d4ed8" : "#64748b", hasStopEvidence ? 5.6 : 3.6,
            `<strong>${{hasStopEvidence ? "停靠站点" : "沿线站点"}}</strong><br>${{escapeHtml(item.name || "未命名站点")}}<br>轨迹关联：${{formatFiles(item.files)}}<br>停靠归属：${{formatFiles(item.stop_files)}}${{statsHtml}}`,
            hasStopEvidence ? "#60a5fa" : "#cbd5e1");
          addLinkedMarker(stationLayer, marker, item.files);
        }});
        data.osm_pois.filter(p => p.kind === "traffic_signal").forEach(item => {{
          const marker = circle(item.lat, item.lon, "#dc2626", 3.8,
            `<strong>红绿灯</strong><br>${{escapeHtml(item.name || "交通信号灯")}}<br>关联 CSV：${{formatFiles(item.files)}}<br>最近轨迹距离：${{item.distance_m}} m`,
            "#fecaca");
          addLinkedMarker(signalLayer, marker, item.files);
        }});

        if (bounds.length) {{
          map.fitBounds(bounds, {{ padding: [28, 28] }});
        }}

        document.getElementById("stats").innerHTML = `
          <div class="stat"><strong>${{data.routes.length}}</strong>CSV 轨迹</div>
          <div class="stat"><strong>${{data.summary.total_raw_points.toLocaleString()}}</strong>原始定位点</div>
          <div class="stat"><strong>${{data.charging_locations.length}}</strong>充电位置</div>
          <div class="stat"><strong>${{data.osm_counts.stop_bus_stop || 0}}</strong>停靠站 / <strong style="display:inline">${{data.osm_counts.traffic_signal}}</strong>灯</div>
        `;
        document.getElementById("legend").innerHTML = `
          <div>${{data.routes.map((route, index) => `<span class="route-chip"><span class="line" style="background:${{colors[index % colors.length]}}"></span>${{escapeHtml(route.file)}}</span>`).join("")}}</div>
          <div>路径使用全量数据；仅合并连续相同经纬度：${{data.summary.total_compressed_points.toLocaleString()}} 个绘制点，合并 ${{data.summary.total_merged_repeats.toLocaleString()}} 条重复记录</div>
          <div><span class="dot" style="background:#0f766e"></span>起点 <span class="dot" style="background:#991b1b;margin-left:12px"></span>终点 <span class="dot" style="background:#f59e0b;margin-left:12px"></span>充电位置</div>
          <div><span class="dot" style="background:#60a5fa"></span>停靠站点 <span class="dot" style="background:#cbd5e1;margin-left:12px"></span>沿线站点 <span class="dot" style="background:#dc2626;margin-left:12px"></span>红绿灯</div>
          <div>CSV 勾选会联动隐藏/显示对应轨迹、充电位置、站点、红绿灯。</div>
        `;
      }});
  </script>
</body>
</html>
"""


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    routes = []
    route_points_by_file = {}
    charging_points = []
    stop_events = []
    total_raw_points = 0
    total_compressed_points = 0
    total_merged_repeats = 0
    bbox = [999, 999, -999, -999]

    for path in sorted(DATA_DIR.glob("*.csv")):
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
        route_points_by_file[path.name] = [(p["lat"], p["lon"]) for p in compressed]
        stop_events.extend(extract_stop_events(rows, path.name))
        for p in rows:
            bbox[0] = min(bbox[0], p["lon"])
            bbox[1] = min(bbox[1], p["lat"])
            bbox[2] = max(bbox[2], p["lon"])
            bbox[3] = max(bbox[3], p["lat"])
        routes.append({
            "file": path.name,
            "raw_count": len(rows),
            "compressed_count": len(compressed),
            "merged_repeated_points": len(rows) - len(compressed),
            "start": rows[0],
            "end": rows[-1],
            "encoded_path": encode_polyline6(compressed),
        })

    ref_lat = (bbox[1] + bbox[3]) / 2
    charging_locations = cluster_points(charging_points, CHARGE_CLUSTER_METERS, "充电位置")
    pois = load_osm_poi(OSM_POI_FILE)
    route_grids_by_file = {
        file_name: build_route_vertex_index(points, ref_lat)
        for file_name, points in route_points_by_file.items()
    }
    osm_pois = annotate_pois_by_routes(pois, route_grids_by_file, ref_lat) if pois else []
    all_bus_stops = [poi for poi in pois if poi["kind"] == "bus_stop"]
    stop_evidence = attach_stop_evidence(osm_pois, stop_events, all_bus_stops, ref_lat) if pois else {}
    osm_counts = {
        "bus_stop": sum(1 for p in osm_pois if p["kind"] == "bus_stop"),
        "stop_bus_stop": sum(1 for p in osm_pois if p["kind"] == "bus_stop" and p.get("stop_files")),
        "traffic_signal": sum(1 for p in osm_pois if p["kind"] == "traffic_signal"),
    }

    output = {
        "summary": {
            "total_raw_points": total_raw_points,
            "total_compressed_points": total_compressed_points,
            "total_merged_repeats": total_merged_repeats,
            "bbox": bbox,
            "poi_source": "OpenStreetMap Overpass API",
            "route_snap_meters": ROUTE_SNAP_METERS,
            "stop_speed_kmh": STOP_SPEED_KMH,
            "stop_min_seconds": STOP_MIN_SECONDS,
            "stop_max_seconds": STOP_MAX_SECONDS,
            "stop_to_bus_stop_meters": STOP_TO_BUS_STOP_METERS,
            "stop_events": len(stop_events),
            "matched_stop_stations": len(stop_evidence),
            "compression": "Only consecutive identical latitude/longitude records are merged. No route sampling is used.",
        },
        "routes": routes,
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
    print(f"Charging clusters: {len(charging_locations)}")
    print(f"Stop events: {len(stop_events)}, matched stop stations: {len(stop_evidence)}")
    print(f"OSM POIs near route: {osm_counts}")


if __name__ == "__main__":
    main()
