"""漂移推算层。

只做与存储、HTTP、页面无关的纯计算：
- 事件发生到当前经过的时长；
- 漂移速度（节）与时长换算漂移距离；
- 按漂移方位（罗经方位，0=正北，顺时针）在球面上推算新中心点；
- 概率区域半径随漂移距离增长（位置不确定性随时间发散）。

调度规则见 scheduling.py，持久化与接口见 app.py。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

EARTH_RADIUS_KM = 6371.0088
KNOT_TO_KMH = 1.852  # 1 节 = 1 海里/小时 = 1.852 公里/小时
# 概率半径相对漂移距离的发散系数：漂移越远，概率圈越大
DEFAULT_SPREAD_FACTOR = 0.10


def parse_iso(value: str | datetime) -> datetime:
    """解析 ISO 时间字符串，补齐 UTC 时区。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def elapsed_hours(since: str | datetime, now: str | datetime | None = None) -> float:
    """事件发生（since）到 now 经过的小时数，回溯时间按 0 处理。"""
    start = parse_iso(since)
    end = parse_iso(now) if now is not None else datetime.now(timezone.utc)
    return max(0.0, (end - start).total_seconds() / 3600.0)


def drift_distance_km(drift_speed_kn: float, hours: float) -> float:
    """漂移速度（节）× 时长（小时）→ 漂移距离（公里）。"""
    return max(0.0, float(drift_speed_kn)) * max(0.0, float(hours)) * KNOT_TO_KMH


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def destination_point(latitude: float, longitude: float,
                      bearing_deg: float, distance_km: float) -> tuple[float, float]:
    """从 (latitude, longitude) 沿罗经方位 bearing_deg 走 distance_km 后的坐标。"""
    if distance_km <= 0:
        return latitude, longitude
    bearing = math.radians(bearing_deg % 360)
    delta = distance_km / EARTH_RADIUS_KM
    phi1 = math.radians(latitude)
    lam1 = math.radians(longitude)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta)
        + math.cos(phi1) * math.sin(delta) * math.cos(bearing)
    )
    lam2 = lam1 + math.atan2(
        math.sin(bearing) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    lat2 = math.degrees(phi2)
    lon2 = (math.degrees(lam2) + 540.0) % 360.0 - 180.0
    return round(lat2, 6), round(lon2, 6)


@dataclass(frozen=True)
class DriftProjection:
    center_lat: float
    center_lon: float
    radius_km: float
    drift_distance_km: float
    elapsed_hours: float
    bearing_deg: float
    speed_kn: float

    def as_dict(self) -> dict:
        return {
            "center_lat": self.center_lat,
            "center_lon": self.center_lon,
            "radius_km": round(self.radius_km, 3),
            "drift_distance_km": round(self.drift_distance_km, 3),
            "elapsed_hours": round(self.elapsed_hours, 4),
            "bearing_deg": self.bearing_deg % 360,
            "speed_kn": self.speed_kn,
        }


def project(latitude: float, longitude: float, radius_km: float,
            drift_direction_deg: float, drift_speed_kn: float, hours: float,
            spread_factor: float = DEFAULT_SPREAD_FACTOR) -> DriftProjection:
    """按事件漂移参数把一个手工圈定区域推算为概率区域。

    中心点沿漂移方位平移漂移距离；半径在原半径上按漂移距离发散增长。
    """
    hours = max(0.0, float(hours))
    distance = drift_distance_km(drift_speed_kn, hours)
    center_lat, center_lon = destination_point(latitude, longitude, drift_direction_deg, distance)
    grown_radius = float(radius_km) + distance * spread_factor
    return DriftProjection(
        center_lat=center_lat,
        center_lon=center_lon,
        radius_km=round(grown_radius, 3),
        drift_distance_km=distance,
        elapsed_hours=hours,
        bearing_deg=float(drift_direction_deg) % 360,
        speed_kn=float(drift_speed_kn),
    )
