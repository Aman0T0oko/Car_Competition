# 生产路线纠偏流水线

这条流水线只解决路线绘制可靠性问题。站点、停车、充电点后续应从原始 CSV 的停车/充电行为单独识别，不参与路线纠偏包。

## 运行

```bash
python3 tools/production_route_pipeline.py --summary
```

## 路线点压缩

路线阶段会丢弃：

- 车速小于等于 `1 km/h` 的停车点
- 充电状态为 `1` 或 `4` 的点
- 连续重复点
- 对路线形状贡献很低的密集点

保留规则：

- 移动点之间距离超过 `25 m` 或间隔超过 `60 s`
- RDP 几何简化阈值为 `18 m`
- 移动点之间超过 `5 min` 无移动时切分为新任务

## 供应商回填格式

脚本会生成纠偏任务包：

```text
outputs/production/correction_jobs/<csv_stem>/<job_id>.json
```

把高德或百度纠偏后的结果放到：

```text
outputs/production/provider_results/amap/<csv_stem>/<job_id>.json
outputs/production/provider_results/baidu/<csv_stem>/<job_id>.json
```

每个回填 JSON 至少包含：

```json
{
  "path": [[30.123456, 120.123456], [30.123789, 120.123789]],
  "distance_m": 1234.5,
  "coverage": 0.98,
  "confidence": 0.97
}
```

`path` 也可以使用对象格式：

```json
{
  "path": [
    {"lat": 30.123456, "lon": 120.123456},
    {"lat": 30.123789, "lon": 120.123789}
  ]
}
```

## 质量准入

- `A`：双引擎一致、覆盖率高、里程与车辆累计里程吻合，可直接使用
- `B`：主引擎可信，但证据略弱，可展示
- `C`：需要人工复核
- `D`：不能作为可用路线

没有供应商回填时，所有路线段都会标记为 `D / missing_provider_mapmatch`。

## 输出

- `outputs/corrected_routes.geojson`：只包含 `A/B` 可用路线
- `outputs/review_segments.geojson`：包含 `C/D` 复核片段
- `outputs/route_quality_report.csv`：每段路线的等级、原因和里程证据
- `outputs/route_pipeline_manifest.json`：本次运行的压缩率、任务数和策略
- `outputs/production_route_review.html`：生产路线复核地图
