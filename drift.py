"""漂移推算：按事件经过时长、漂移方向和漂移速度推算概率区域。

纯函数模块，不依赖数据库与 HTTP 层，便于单独测试和替换推算模型。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

EARTH_RADIUS_KM = 6371.0088
KNOT_TO_KMH = 1.852
#: 漂移推算误差按漂移距离的比例放大
DRIFT_UNCERTAINTY_RATIO = 0.10
#: 概率区域半径随时间的自然扩散速度（公里/小时）
HOURLY_SPREAD_KM = 0.5


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def parse_instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant


def elapsed_hours(start_iso: str, now: datetime | None = None) -> float:
    """事件发生到当前时刻经过的小时数（不为负）。"""
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - parse_instant(start_iso)).total_seconds() / 3600.0)


def project_position(lat: float, lon: float, bearing_deg: float, distance: float) -> tuple[float, float]:
    """从 (lat, lon) 沿大圆方位角 bearing_deg 移动 distance 公里后的坐标。"""
    if distance <= 0:
        return float(lat), float(lon)
    delta = distance / EARTH_RADIUS_KM
    theta = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    phi2 = math.asin(math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta))
    lam2 = math.radians(lon) + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), (math.degrees(lam2) + 540.0) % 360.0 - 180.0


def project_probability_area(lat: float, lon: float, uncertainty_km: float,
                             drift_direction: float, drift_speed_kn: float,
                             hours: float) -> dict[str, float]:
    """按漂移推算概率区域。

    中心 = 事发位置沿漂移方向移动（漂移速度 × 经过时长）；
    半径 = 初始不确定半径 + 漂移距离推算误差 + 随时间的自然扩散。
    """
    hours = max(0.0, hours)
    drift_km = max(0.0, drift_speed_kn) * KNOT_TO_KMH * hours
    center_lat, center_lon = project_position(lat, lon, drift_direction, drift_km)
    radius_km = uncertainty_km + drift_km * DRIFT_UNCERTAINTY_RATIO + HOURLY_SPREAD_KM * hours
    return {
        "center_lat": center_lat,
        "center_lon": center_lon,
        "radius_km": radius_km,
        "drift_distance_km": drift_km,
        "elapsed_hours": hours,
    }
