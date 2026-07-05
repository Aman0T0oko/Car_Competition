#!/usr/bin/env python3
import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "浙江杭州"
OUTPUT_DIR = ROOT / "outputs"
PRODUCTION_DIR = OUTPUT_DIR / "production"
JOB_DIR = PRODUCTION_DIR / "correction_jobs"
PROVIDER_DIR = PRODUCTION_DIR / "provider_results"
REPORT_FILE = OUTPUT_DIR / "route_quality_report.csv"
CORRECTED_GEOJSON = OUTPUT_DIR / "corrected_routes.geojson"
REVIEW_GEOJSON = OUTPUT_DIR / "review_segments.geojson"
MANIFEST_FILE = OUTPUT_DIR / "route_pipeline_manifest.json"
REVIEW_HTML = OUTPUT_DIR / "production_route_review.html"

LON_FIELD = "经度"
LAT_FIELD = "纬度"
TIME_FIELD = "数据时间"
SPEED_FIELD = "车速 km/h"
CHARGE_FIELD = "充电状态"
MILEAGE_FIELD = "累计里程 km"

MAX_JOB_POINTS = 950
MAX_JOB_SECONDS = 20 * 60
MAX_GAP_SECONDS = 5 * 60
STOP_SPEED_KMH = 1.0
MIN_MOVING_POINTS = 5
MIN_MOVING_METERS = 150
MIN_ROUTE_SAMPLE_METERS = 25
MAX_ROUTE_SAMPLE_SECONDS = 60
RDP_EPSILON_METERS = 18
MIN_ACCEPTED_COVERAGE = 0.92
MIN_ENGINE_AGREEMENT = 0.85
MAX_DISTANCE_DELTA_RATIO = 0.12


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


def path_distance_m(path):
    return sum(haversine_m(a, b) for a, b in zip(path, path[1:]))


def project(lon, lat, ref_lat):
    return (
        lon * 111320 * math.cos(math.radians(ref_lat)),
        lat * 110540,
    )


def point_line_distance_m(point, start, end, ref_lat):
    px, py = project(point[1], point[0], ref_lat)
    ax, ay = project(start[1], start[0], ref_lat)
    bx, by = project(end[1], end[0], ref_lat)
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def read_points(path):
    points = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for index, row in enumerate(reader, start=1):
            lon = parse_float(row.get(LON_FIELD))
            lat = parse_float(row.get(LAT_FIELD))
            timestamp = parse_time(row.get(TIME_FIELD))
            if not valid_coord(lon, lat) or timestamp is None:
                continue
            points.append({
                "index": index,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "time": timestamp,
                "time_text": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "speed": parse_float(row.get(SPEED_FIELD), 0) or 0,
                "charge": str(row.get(CHARGE_FIELD, "")).strip(),
                "mileage": parse_float(row.get(MILEAGE_FIELD)),
            })
    return points


def is_moving(point):
    return point["speed"] > STOP_SPEED_KMH and point["charge"] not in {"1", "4"}


def point_gap_seconds(left, right):
    return int((right["time"] - left["time"]).total_seconds())


def split_moving_runs(points):
    runs = []
    current = []

    def finish():
        nonlocal current
        if len(current) >= MIN_MOVING_POINTS:
            runs.append(current)
        current = []

    for point in points:
        if not is_moving(point):
            continue
        if current and point_gap_seconds(current[-1], point) > MAX_GAP_SECONDS:
            finish()
        current.append(point)
    finish()
    return runs


def rdp_indexes(points, epsilon_m):
    if len(points) <= 2:
        return set(range(len(points)))
    ref_lat = sum(p["lat"] for p in points) / len(points)
    path = [[p["lat"], p["lon"]] for p in points]

    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end - start <= 1:
            continue
        best_index = None
        best_distance = -1
        for index in range(start + 1, end):
            dist = point_line_distance_m(path[index], path[start], path[end], ref_lat)
            if dist > best_distance:
                best_distance = dist
                best_index = index
        if best_distance > epsilon_m and best_index is not None:
            keep.add(best_index)
            stack.append((start, best_index))
            stack.append((best_index, end))
    return keep


def route_sample_points(points):
    sampled = []
    last_kept = None
    for point in points:
        if last_kept is None:
            sampled.append(point)
            last_kept = point
            continue
        if point["lat"] == last_kept["lat"] and point["lon"] == last_kept["lon"]:
            continue
        dist = haversine_m((last_kept["lat"], last_kept["lon"]), (point["lat"], point["lon"]))
        elapsed = point_gap_seconds(last_kept, point)
        if dist >= MIN_ROUTE_SAMPLE_METERS or elapsed >= MAX_ROUTE_SAMPLE_SECONDS:
            sampled.append(point)
            last_kept = point
    if points and sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    keep = rdp_indexes(sampled, RDP_EPSILON_METERS)
    simplified = [point for index, point in enumerate(sampled) if index in keep]
    if len(simplified) < MIN_MOVING_POINTS:
        return sampled
    return simplified


def build_route_points(points):
    route_points = []
    route_runs = 0
    for run in split_moving_runs(points):
        raw_path = [[p["lat"], p["lon"]] for p in run]
        if path_distance_m(raw_path) < MIN_MOVING_METERS:
            continue
        route_runs += 1
        route_points.extend(route_sample_points(run))
    return route_points, route_runs


def split_jobs(points):
    jobs = []
    current = []

    def finish():
        nonlocal current
        if not current:
            return
        moving_points = [p for p in current if is_moving(p)]
        raw_path = [[p["lat"], p["lon"]] for p in current]
        moving_distance = path_distance_m(raw_path)
        if len(moving_points) >= MIN_MOVING_POINTS and moving_distance >= MIN_MOVING_METERS:
            jobs.append(build_job_record(len(jobs) + 1, current, moving_distance, len(moving_points)))
        current = []

    for point in points:
        if current:
            gap_s = point_gap_seconds(current[-1], point)
            duration_s = point_gap_seconds(current[0], point)
            if gap_s > MAX_GAP_SECONDS or len(current) >= MAX_JOB_POINTS or duration_s > MAX_JOB_SECONDS:
                finish()
        current.append(point)
    finish()
    return jobs


def build_job_record(number, points, moving_distance, moving_points):
    first = points[0]
    last = points[-1]
    raw_path = [[p["lat"], p["lon"]] for p in points]
    mileage_delta = None
    if first["mileage"] is not None and last["mileage"] is not None and last["mileage"] >= first["mileage"]:
        mileage_delta = round((last["mileage"] - first["mileage"]) * 1000, 1)
    return {
        "job_id": f"{number:05d}",
        "start_time": first["time_text"],
        "end_time": last["time_text"],
        "raw_points": len(points),
        "moving_points": moving_points,
        "raw_distance_m": round(path_distance_m(raw_path), 1),
        "moving_distance_m": round(moving_distance, 1),
        "mileage_delta_m": mileage_delta,
        "points": [{
            "lon": p["lon"],
            "lat": p["lat"],
            "locatetime": int(p["time"].timestamp() * 1000),
            "time": p["time_text"],
            "speed": p["speed"],
            "mileage": p["mileage"],
        } for p in points],
    }


def write_provider_jobs(file_name, jobs):
    stem = Path(file_name).stem
    route_dir = JOB_DIR / stem
    route_dir.mkdir(parents=True, exist_ok=True)
    for old_job in route_dir.glob("*.json"):
        old_job.unlink()
    for job in jobs:
        payload = {
            "file": file_name,
            "job_id": job["job_id"],
            "provider_hints": {
                "amap": {
                    "correction": "denoise=1,mapmatch=1,attribute=1,threshold=20,mode=driving",
                    "recoup": 1,
                    "gap": 50,
                },
                "baidu": {
                    "is_processed": 1,
                    "process_option": "denoise_grade=5,need_mapmatch=1,transport_mode=driving",
                    "supplement_mode": "driving",
                    "coord_type_output": "gcj02",
                },
            },
            "source": {
                "start_time": job["start_time"],
                "end_time": job["end_time"],
                "raw_points": job["raw_points"],
                "moving_points": job["moving_points"],
                "raw_distance_m": job["raw_distance_m"],
                "mileage_delta_m": job["mileage_delta_m"],
            },
            "points": job["points"],
        }
        out = route_dir / f"{job['job_id']}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def load_provider_result(provider, file_name, job_id):
    path = PROVIDER_DIR / provider / Path(file_name).stem / f"{job_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    path_points = data.get("path") or data.get("points") or []
    normalized = []
    for point in path_points:
        if isinstance(point, dict):
            lat = parse_float(point.get("lat") or point.get("latitude"))
            lon = parse_float(point.get("lon") or point.get("lng") or point.get("longitude"))
        else:
            lat = parse_float(point[0] if len(point) > 0 else None)
            lon = parse_float(point[1] if len(point) > 1 else None)
        if valid_coord(lon, lat):
            normalized.append([round(lat, 7), round(lon, 7)])
    if len(normalized) < 2:
        return None
    return {
        "provider": provider,
        "path": normalized,
        "distance_m": round(parse_float(data.get("distance_m"), path_distance_m(normalized)), 1),
        "coverage": parse_float(data.get("coverage"), 1.0),
        "confidence": parse_float(data.get("confidence"), 1.0),
        "raw": data,
    }


def sample_path(path, max_points=80):
    if len(path) <= max_points:
        return path
    step = (len(path) - 1) / (max_points - 1)
    return [path[round(i * step)] for i in range(max_points)]


def path_agreement(left, right):
    left_sample = sample_path(left)
    right_sample = sample_path(right)
    if not left_sample or not right_sample:
        return 0
    distances = []
    for point in left_sample:
        distances.append(min(haversine_m(point, other) for other in right_sample))
    avg = sum(distances) / len(distances)
    return round(max(0, min(1, 1 - avg / 80)), 4)


def route_grade(provider_results, job):
    reasons = []
    if not provider_results:
        return "D", 0, ["missing_provider_mapmatch"]
    primary = provider_results[0]
    coverage = min(parse_float(primary.get("coverage"), 0), parse_float(primary.get("confidence"), 0))
    score = coverage
    if coverage < MIN_ACCEPTED_COVERAGE:
        reasons.append("low_provider_coverage")

    if len(provider_results) >= 2:
        agreement = path_agreement(provider_results[0]["path"], provider_results[1]["path"])
        score = min(score, agreement)
        if agreement < MIN_ENGINE_AGREEMENT:
            reasons.append("engine_disagreement")
        distances = [item["distance_m"] for item in provider_results]
        distance_delta_ratio = abs(distances[0] - distances[1]) / max(distances)
        if distance_delta_ratio > MAX_DISTANCE_DELTA_RATIO:
            reasons.append("provider_distance_delta")
            score = min(score, 1 - distance_delta_ratio)
    else:
        reasons.append("single_provider_only")
        score = min(score, 0.74)

    if job["mileage_delta_m"] and primary["distance_m"] > 0:
        mileage_delta_ratio = abs(primary["distance_m"] - job["mileage_delta_m"]) / max(primary["distance_m"], job["mileage_delta_m"])
        if mileage_delta_ratio > MAX_DISTANCE_DELTA_RATIO:
            reasons.append("vehicle_mileage_delta")
            score = min(score, 1 - mileage_delta_ratio)

    if score >= 0.92 and not reasons:
        return "A", round(score, 4), []
    if score >= 0.82 and not any(r in reasons for r in {"engine_disagreement", "vehicle_mileage_delta"}):
        return "B", round(score, 4), reasons
    if score >= 0.55:
        return "C", round(score, 4), reasons or ["needs_manual_review"]
    return "D", round(max(0, score), 4), reasons or ["unusable"]


def line_feature(path, properties):
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [[lon, lat] for lat, lon in path],
        },
        "properties": properties,
    }


def point_feature(point, properties):
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [point[1], point[0]],
        },
        "properties": properties,
    }


def build_outputs():
    corrected_features = []
    review_features = []
    report_rows = []
    manifest_routes = []

    for csv_path in sorted(DATA_DIR.glob("*.csv")):
        print(f"Preparing {csv_path.name}", flush=True)
        points = read_points(csv_path)
        route_points, route_runs = build_route_points(points)
        jobs = split_jobs(route_points)
        write_provider_jobs(csv_path.name, jobs)
        route_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
        route_dir = PROVIDER_DIR
        for job in jobs:
            provider_results = [
                item for item in (
                    load_provider_result("amap", csv_path.name, job["job_id"]),
                    load_provider_result("baidu", csv_path.name, job["job_id"]),
                ) if item
            ]
            grade, score, reasons = route_grade(provider_results, job)
            route_counts[grade] += 1
            chosen = provider_results[0] if provider_results else None
            properties = {
                "file": csv_path.name,
                "job_id": job["job_id"],
                "grade": grade,
                "score": score,
                "status": "accepted" if grade in {"A", "B"} else "review_required",
                "reasons": ",".join(reasons),
                "providers": ",".join(item["provider"] for item in provider_results),
                "start_time": job["start_time"],
                "end_time": job["end_time"],
                "raw_points": job["raw_points"],
                "moving_points": job["moving_points"],
                "raw_distance_m": job["raw_distance_m"],
                "mileage_delta_m": job["mileage_delta_m"],
                "corrected_distance_m": chosen["distance_m"] if chosen else None,
            }
            if chosen and grade in {"A", "B"}:
                corrected_features.append(line_feature(chosen["path"], properties))
            else:
                raw_path = [[p["lat"], p["lon"]] for p in job["points"]]
                review_features.append(line_feature(raw_path, properties))
                review_features.append(point_feature(raw_path[0], dict(properties, marker="start")))
                review_features.append(point_feature(raw_path[-1], dict(properties, marker="end")))
            report_rows.append(properties)
        manifest_routes.append({
            "file": csv_path.name,
            "input_points": len(points),
            "route_points": len(route_points),
            "route_point_reduction_ratio": round(1 - (len(route_points) / len(points)), 4) if points else 0,
            "moving_runs": route_runs,
            "correction_jobs": len(jobs),
            "job_payload_dir": str((JOB_DIR / csv_path.stem).relative_to(ROOT)),
            "provider_result_dir": str((route_dir / "<provider>" / csv_path.stem).relative_to(ROOT)),
            "grades": route_counts,
        })

    write_geojson(CORRECTED_GEOJSON, corrected_features)
    write_geojson(REVIEW_GEOJSON, review_features)
    write_report(report_rows)
    REVIEW_HTML.write_text(build_review_html(), encoding="utf-8")
    manifest = {
        "status": "provider_results_required" if not corrected_features else "completed_with_provider_results",
        "policy": {
            "accepted_grades": ["A", "B"],
            "review_grades": ["C", "D"],
            "route_only_sampling": {
                "stop_speed_kmh": STOP_SPEED_KMH,
                "min_route_sample_meters": MIN_ROUTE_SAMPLE_METERS,
                "max_route_sample_seconds": MAX_ROUTE_SAMPLE_SECONDS,
                "rdp_epsilon_meters": RDP_EPSILON_METERS,
                "charging_states_excluded_from_route": ["1", "4"],
            },
            "min_accepted_coverage": MIN_ACCEPTED_COVERAGE,
            "min_engine_agreement": MIN_ENGINE_AGREEMENT,
            "max_distance_delta_ratio": MAX_DISTANCE_DELTA_RATIO,
        },
        "provider_result_schema": {
            "path": "required list of [lat, lon] points or objects with lat/lon",
            "distance_m": "optional corrected distance in meters",
            "coverage": "optional 0..1 provider point/path coverage",
            "confidence": "optional 0..1 provider confidence",
        },
        "routes": manifest_routes,
        "outputs": {
            "corrected_routes_geojson": str(CORRECTED_GEOJSON.relative_to(ROOT)),
            "review_segments_geojson": str(REVIEW_GEOJSON.relative_to(ROOT)),
            "route_quality_report_csv": str(REPORT_FILE.relative_to(ROOT)),
            "review_html": str(REVIEW_HTML.relative_to(ROOT)),
        },
    }
    MANIFEST_FILE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def write_geojson(path, features):
    payload = {
        "type": "FeatureCollection",
        "features": features,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_report(rows):
    fieldnames = [
        "file",
        "job_id",
        "grade",
        "score",
        "status",
        "reasons",
        "providers",
        "start_time",
        "end_time",
        "raw_points",
        "moving_points",
        "raw_distance_m",
        "mileage_delta_m",
        "corrected_distance_m",
    ]
    with REPORT_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_review_html():
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>生产路线复核</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map { height: 100%; margin: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .panel {
      position: absolute;
      right: 12px;
      bottom: 20px;
      z-index: 900;
      width: min(360px, calc(100vw - 24px));
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid #d4dae5;
      border-radius: 8px;
      box-shadow: 0 10px 28px rgba(31, 41, 55, 0.18);
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
  <aside class="panel" id="panel">
    <strong>生产路线复核</strong>
    <div class="row"><span>A/B 可用路线</span><span id="acceptedCount">0</span></div>
    <div class="row"><span>C/D 复核片段</span><span id="reviewCount">0</span></div>
    <div id="routeBreakdown"></div>
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

    function bindPopup(feature, layer) {
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

    Promise.all([
      fetch("corrected_routes.geojson").then(r => r.json()),
      fetch("review_segments.geojson").then(r => r.json())
    ]).then(([accepted, review]) => {
      const acceptedLines = accepted.features.filter(f => f.geometry.type === "LineString");
      const reviewLines = review.features.filter(f => f.geometry.type === "LineString");
      const files = Array.from(new Set(
        acceptedLines.concat(reviewLines).map(f => f.properties.file)
      )).sort();
      const overlays = {};
      const visibleLayers = [];
      const breakdown = [];

      files.forEach(file => {
        const acceptedForFile = acceptedLines.filter(f => f.properties.file === file);
        const reviewForFile = reviewLines.filter(f => f.properties.file === file);
        const color = colorFor(file);
        if (acceptedForFile.length) {
          const layer = L.geoJSON({ type: "FeatureCollection", features: acceptedForFile }, {
            style: feature => ({
              color,
              weight: feature.properties.grade === "A" ? 4 : 3,
              opacity: 0.9
            }),
            onEachFeature: bindPopup
          }).addTo(map);
          overlays[`${file} A/B 可用`] = layer;
          visibleLayers.push(layer);
        }
        if (reviewForFile.length) {
          const layer = L.geoJSON({ type: "FeatureCollection", features: reviewForFile }, {
            style: () => ({
              color,
              weight: 2,
              opacity: 0.52
            }),
            onEachFeature: bindPopup
          }).addTo(map);
          overlays[`${file} C/D 复核`] = layer;
          visibleLayers.push(layer);
        }
        breakdown.push(`<div class="row"><span><span style="display:inline-block;width:22px;height:4px;border-radius:3px;background:${color};vertical-align:2px;margin-right:7px"></span>${file}</span><span>${acceptedForFile.length}/${reviewForFile.length}</span></div>`);
      });

      L.control.layers({"OpenStreetMap": base}, overlays, { collapsed: false }).addTo(map);
      document.getElementById("acceptedCount").textContent = acceptedLines.length.toLocaleString();
      document.getElementById("reviewCount").textContent = reviewLines.length.toLocaleString();
      document.getElementById("routeBreakdown").innerHTML = breakdown.join("");
      const bounds = L.featureGroup(visibleLayers).getBounds();
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24] });
      else map.setView([30.27, 120.18], 12);
    });
  </script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Build production route correction jobs and quality-gated outputs.")
    parser.add_argument("--summary", action="store_true", help="Print only a concise summary after writing outputs.")
    args = parser.parse_args()
    PRODUCTION_DIR.mkdir(exist_ok=True)
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    PROVIDER_DIR.mkdir(parents=True, exist_ok=True)
    manifest = build_outputs()
    if args.summary:
        print(json.dumps({
            "status": manifest["status"],
            "routes": [
                {
                    "file": route["file"],
                    "input_points": route["input_points"],
                    "route_points": route["route_points"],
                    "reduction": route["route_point_reduction_ratio"],
                    "jobs": route["correction_jobs"],
                    "grades": route["grades"],
                }
                for route in manifest["routes"]
            ],
            "outputs": manifest["outputs"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
