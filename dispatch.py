"""调度规则：概率区域重叠合并与资源航程放行判断。

只依赖 drift.distance_km，不接触数据库与 HTTP，规则可独立调整。
"""
from __future__ import annotations

import json
from typing import Any

from drift import distance_km


def circles_overlap(a: dict[str, float], b: dict[str, float]) -> bool:
    """两个概率圆是否重叠（圆心距不超过半径之和）。"""
    gap = distance_km(a["center_lat"], a["center_lon"], b["center_lat"], b["center_lon"])
    return gap <= a["radius_km"] + b["radius_km"]


def merge_circles(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """返回能同时盖住两个圆的最小包围圆。"""
    gap = distance_km(a["center_lat"], a["center_lon"], b["center_lat"], b["center_lon"])
    if gap + min(a["radius_km"], b["radius_km"]) <= max(a["radius_km"], b["radius_km"]):
        return dict(a if a["radius_km"] >= b["radius_km"] else b)
    radius = (gap + a["radius_km"] + b["radius_km"]) / 2.0
    t = 0.0 if gap == 0 else (radius - a["radius_km"]) / gap
    return {
        "center_lat": a["center_lat"] + (b["center_lat"] - a["center_lat"]) * t,
        "center_lon": a["center_lon"] + (b["center_lon"] - a["center_lon"]) * t,
        "radius_km": radius,
    }


def capable_assets(assets: list[dict[str, Any]], area_kind: str, sea_state: int) -> list[dict[str, Any]]:
    """当前可用、具备区域能力且适应该海况的资源。"""
    result = []
    for asset in assets:
        if asset["status"] != "available":
            continue
        if asset["max_sea_state"] < sea_state:
            continue
        if area_kind not in json.loads(asset["capabilities"]):
            continue
        result.append(asset)
    return result


def unreachable_assets(assets: list[dict[str, Any]], area_kind: str, sea_state: int,
                       center_lat: float, center_lon: float) -> list[dict[str, Any]]:
    """列出够不着该区域中心的候选资源（含航程差距）。"""
    misses = []
    for asset in capable_assets(assets, area_kind, sea_state):
        dist = distance_km(asset["latitude"], asset["longitude"], center_lat, center_lon)
        if dist > asset["range_km"]:
            misses.append({
                "asset_id": asset["id"],
                "name": asset["name"],
                "distance_km": round(dist, 1),
                "range_km": asset["range_km"],
                "shortfall_km": round(dist - asset["range_km"], 1),
            })
    return misses


def hold_reason_text(misses: list[dict[str, Any]]) -> str:
    """待放行原因：写清是哪条资源够不着。"""
    if not misses:
        return ""
    detail = "、".join(
        "%s（距离%.1fkm，航程%.1fkm）" % (m["name"], m["distance_km"], m["range_km"]) for m in misses
    )
    return "超出资源航程：" + detail
