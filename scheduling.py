"""调度规则层。

只做与存储、HTTP、页面无关的纯规则判断：
- 两个概率圆形区域是否重叠；
- 两个重叠圆合并后的概率圆（最小外接圆近似：取过两圆圆心直线上
  四个极值点的外接圆，对包含关系同样成立）；
- 哪些资源有资格承担该类型搜索（能力匹配、海况允许）；
- 对有资格的资源逐个做航程门控，判断概率区域可否放行，并写清
  是哪条资源够不着、还差多少公里。

漂移推算见 drift.py，持久化与接口见 app.py。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from drift import destination_point, haversine_km

# 概率区域状态
PLANNED = "planned"            # 可放行，等待分配资源
PENDING_RELEASE = "pending_release"  # 推算完成但没有任何够得着的资源
REVIEW = "review"              # 待复核：已被新推算区取代的旧区域
MERGED = "merged"              # 已并入其他概率区域
ASSIGNED = "assigned"          # 已分配
ACTIVE = "active"              # 执行中
COMPLETED = "completed"
ABANDONED = "abandoned"

# 手工圈定区域保存后立即转入的状态
SUPERSEDED_STATUS = REVIEW
# 仍可参与重叠合并的概率区域状态
MERGEABLE_STATUS = (PLANNED, PENDING_RELEASE)
# 仍在占用事件、未结束的区域状态
OPEN_STATUS = (PLANNED, PENDING_RELEASE, REVIEW, ASSIGNED, ACTIVE)


@dataclass
class Circle:
    center_lat: float
    center_lon: float
    radius_km: float


def circles_overlap(a: Circle, b: Circle) -> bool:
    """两圆相交或相切即视为重叠（圆心距不超过半径之和）。"""
    gap = haversine_km(a.center_lat, a.center_lon, b.center_lat, b.center_lon)
    return gap <= a.radius_km + b.radius_km


def merge_circles(a: Circle, b: Circle) -> Circle:
    """两个重叠概率圆合并为一个概率圆。

    包含关系时合并结果就是较大圆；否则取 a→b 方位线上两圆各自背向边缘
    的点 p、q，合并圆以其球面中点为圆心、半距为半径（最小外接圆近似）。
    """
    gap = haversine_km(a.center_lat, a.center_lon, b.center_lat, b.center_lon)
    # 包含关系：合并结果退化为较大圆
    if gap + b.radius_km <= a.radius_km:
        return Circle(a.center_lat, a.center_lon, a.radius_km)
    if gap + a.radius_km <= b.radius_km:
        return Circle(b.center_lat, b.center_lon, b.radius_km)
    bearing = _bearing_between(a, b)
    # p：a 圆背向 b 的边缘点；q：b 圆背向 a 的边缘点
    p_lat, p_lon = destination_point(a.center_lat, a.center_lon, bearing + 180.0, a.radius_km)
    q_lat, q_lon = destination_point(b.center_lat, b.center_lon, bearing, b.radius_km)
    diameter = haversine_km(p_lat, p_lon, q_lat, q_lon)
    c_lat, c_lon = destination_point(p_lat, p_lon, bearing, diameter / 2.0)
    return Circle(c_lat, c_lon, round(diameter / 2.0, 3))


def _bearing_between(a: Circle, b: Circle) -> float:
    phi1, phi2 = math.radians(a.center_lat), math.radians(b.center_lat)
    dl = math.radians(b.center_lon - a.center_lon)
    y = math.sin(dl) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def eligible_assets(area_kind: str, sea_state: int, assets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """能力覆盖该搜索类型、且海况在其适用范围内的资源。"""
    result = []
    for asset in assets:
        capabilities = asset.get("capabilities")
        if isinstance(capabilities, str):
            import json
            capabilities = json.loads(capabilities)
        if not capabilities or area_kind not in capabilities:
            continue
        if int(sea_state) > int(asset["max_sea_state"]):
            continue
        result.append(asset)
    return result


@dataclass
class GateCheck:
    reachable: bool
    status: str
    details: list[dict[str, Any]] = field(default_factory=list)

    def unreachable_assets(self) -> list[str]:
        return [item["asset_name"] for item in self.details if not item["reachable"]]


def evaluate_release_gate(circle: Circle, area_kind: str, sea_state: int,
                          assets: Sequence[dict[str, Any]]) -> GateCheck:
    """航程放行门控。

    仅对有资格（能力、海况）的资源判断：资源到概率区中心的距离不超过
    其航程才算够得着。只要有一条够得着即可放行；全部够不着则留在
    待放行，并在 details 中逐条写清缺口里程。
    """
    candidates = eligible_assets(area_kind, sea_state, assets)
    details = []
    for asset in candidates:
        distance = haversine_km(
            float(asset["latitude"]), float(asset["longitude"]),
            circle.center_lat, circle.center_lon,
        )
        range_km = float(asset["range_km"])
        details.append({
            "asset_id": int(asset["id"]),
            "asset_name": asset["name"],
            "range_km": round(range_km, 3),
            "distance_km": round(distance, 3),
            "shortfall_km": round(max(0.0, distance - range_km), 3),
            "reachable": distance <= range_km,
        })
    reachable = any(item["reachable"] for item in details)
    details.sort(key=lambda item: (not item["reachable"], item["shortfall_km"], item["distance_km"]))
    return GateCheck(
        reachable=reachable,
        status=PLANNED if reachable else PENDING_RELEASE,
        details=details,
    )


def merge_priority(priorities: Iterable[int]) -> int:
    """合并区域取最高优先级（数值最小）。"""
    values = list(priorities)
    return min(values) if values else 3
