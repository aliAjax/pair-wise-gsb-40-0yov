import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, MaritimeSARService  # noqa: E402
from drift import (  # noqa: E402
    KNOT_TO_KMH,
    destination_point,
    drift_distance_km,
    elapsed_hours,
    haversine_km,
    project,
)
from scheduling import (  # noqa: E402
    Circle,
    circles_overlap,
    eligible_assets,
    evaluate_release_gate,
    merge_circles,
)


class DriftProjectionTest(unittest.TestCase):
    def test_elapsed_hours_and_drift_distance(self):
        self.assertEqual(2.0, elapsed_hours("2026-09-25T00:00:00+00:00", "2026-09-25T02:00:00+00:00"))
        self.assertEqual(0.0, elapsed_hours("2026-09-25T02:00:00+00:00", "2026-09-25T00:00:00+00:00"))
        self.assertAlmostEqual(2 * 2 * KNOT_TO_KMH, drift_distance_km(2, 2))

    def test_destination_point_bearing(self):
        lat, lon = destination_point(31.0, 122.0, 90, 10.0)
        self.assertAlmostEqual(31.0, lat, places=3)
        self.assertGreater(lon, 122.0)
        lat, lon = destination_point(31.0, 122.0, 0, 10.0)
        self.assertGreater(lat, 31.0)
        self.assertAlmostEqual(122.0, lon, places=3)
        self.assertEqual((31.0, 122.0), destination_point(31.0, 122.0, 45, 0))

    def test_project_moves_center_and_grows_radius(self):
        p = project(31.0, 122.0, 10.0, 90, 2.0, 2.0)
        distance = 2.0 * 2.0 * KNOT_TO_KMH
        self.assertAlmostEqual(distance, p.drift_distance_km)
        self.assertAlmostEqual(10.0 + distance * 0.10, p.radius_km, places=3)
        self.assertAlmostEqual(31.0, p.center_lat, places=3)
        self.assertGreater(p.center_lon, 122.0)
        self.assertAlmostEqual(distance, haversine_km(31.0, 122.0, p.center_lat, p.center_lon), places=2)


class SchedulingRulesTest(unittest.TestCase):
    def test_circles_overlap(self):
        a = Circle(31.0, 122.0, 10.0)
        b = Circle(31.0, 122.1, 10.0)   # 圆心距约 9.5 km，半径和 20 km
        c = Circle(31.0, 123.0, 10.0)   # 圆心距约 95 km
        self.assertTrue(circles_overlap(a, b))
        self.assertFalse(circles_overlap(a, c))

    def test_merge_circles_contains_both(self):
        a = Circle(31.0, 122.0, 10.0)
        b = Circle(31.0, 122.1, 12.0)
        merged = merge_circles(a, b)
        for circle in (a, b):
            gap = haversine_km(merged.center_lat, merged.center_lon, circle.center_lat, circle.center_lon)
            self.assertLessEqual(gap + circle.radius_km, merged.radius_km + 0.05)

    def test_merge_circles_containment_keeps_bigger(self):
        big = Circle(31.0, 122.0, 30.0)
        small = Circle(31.01, 122.01, 5.0)
        merged = merge_circles(big, small)
        self.assertAlmostEqual(big.radius_km, merged.radius_km, places=1)
        self.assertAlmostEqual(big.center_lat, merged.center_lat, places=2)

    def test_eligible_assets_filters_capability_and_sea_state(self):
        assets = [
            {"id": 1, "name": "海巡01", "capabilities": ["surface"], "max_sea_state": 6,
             "latitude": 31.0, "longitude": 122.0, "range_km": 100},
            {"id": 2, "name": "直升机", "capabilities": ["air"], "max_sea_state": 5,
             "latitude": 31.0, "longitude": 122.0, "range_km": 200},
            {"id": 3, "name": "小艇", "capabilities": ["surface"], "max_sea_state": 2,
             "latitude": 31.0, "longitude": 122.0, "range_km": 50},
        ]
        names = [a["name"] for a in eligible_assets("surface", 3, assets)]
        self.assertEqual(["海巡01"], names)

    def test_release_gate_reachable_and_unreachable(self):
        circle = Circle(31.0, 122.5, 10.0)
        near = {"id": 1, "name": "海巡01", "capabilities": ["surface"], "max_sea_state": 6,
                "latitude": 31.0, "longitude": 122.0, "range_km": 100}
        gate = evaluate_release_gate(circle, "surface", 3, [near])
        self.assertTrue(gate.reachable)
        self.assertEqual("planned", gate.status)

        far = dict(near, range_km=10)
        gate = evaluate_release_gate(circle, "surface", 3, [far])
        self.assertFalse(gate.reachable)
        self.assertEqual("pending_release", gate.status)
        self.assertEqual(["海巡01"], gate.unreachable_assets())
        self.assertGreater(gate.details[0]["shortfall_km"], 0)

        gate = evaluate_release_gate(circle, "air", 3, [near])
        self.assertFalse(gate.reachable)
        self.assertEqual([], gate.details)


class DriftSchedulingServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = MaritimeSARService(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _incident(self, drift_direction=90, drift_speed=2.0, sea_state=3):
        return self.service.create_incident(
            "coord1", "coordinator", "SAR-D1", "海燕号", 31.0, 122.0, 10.0,
            sea_state, "东海中心", drift_direction=drift_direction, drift_speed_kn=drift_speed,
        )

    def _asset(self, name="海巡01", caps=None, lat=31.0, lon=122.0, range_km=100.0):
        return self.service.add_asset(
            "coord1", "coordinator", name, "vessel", caps or ["surface"], lat, lon, 20, range_km, 6
        )

    def _backdate(self, incident_id, hours):
        from datetime import datetime, timedelta, timezone
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self.service.connect() as conn:
            conn.execute("UPDATE incidents SET created_at=? WHERE id=?", (stamp, incident_id))

    def test_save_projects_probability_area_and_supersedes_manual(self):
        incident = self._incident()
        self._asset()
        self._backdate(incident["id"], 2.0)
        result = self.service.create_search_area(
            "coord1", "coordinator", incident["id"], "A-01", "surface", 31.0, 122.0, 10.0, 1
        )
        manual, prob = result["saved_area"], result["probability_area"]
        # 旧圈定区域转待复核
        self.assertEqual("manual", manual["area_role"])
        self.assertEqual("review", manual["status"])
        # 概率区域按漂移推算：中心东移、半径发散
        self.assertEqual("probability", prob["area_role"])
        self.assertEqual(manual["id"], prob["parent_area_id"])
        self.assertEqual("planned", prob["status"])
        self.assertAlmostEqual(2.0, prob["elapsed_hours"], delta=0.05)
        expected_distance = 2.0 * prob["elapsed_hours"] * KNOT_TO_KMH
        self.assertAlmostEqual(expected_distance, prob["drift_distance_km"], delta=0.5)
        self.assertAlmostEqual(10.0 + prob["drift_distance_km"] * 0.10, prob["radius_km"], places=2)
        self.assertGreater(prob["center_lon"], 122.0)
        self.assertAlmostEqual(31.0, prob["center_lat"], places=3)
        # 时间线记录推算与放行判断
        actions = [t["action"] for t in self.service.incident_timeline(incident["id"])]
        self.assertIn("area.projected", actions)
        self.assertIn("area.release_gate", actions)

    def test_overlapping_probability_areas_merge(self):
        incident = self._incident(drift_speed=0.0)
        self._asset()
        first = self.service.create_search_area(
            "coord1", "coordinator", incident["id"], "A-01", "surface", 31.0, 122.0, 10.0, 1
        )
        self.assertEqual([], first["merges"])
        second = self.service.create_search_area(
            "coord1", "coordinator", incident["id"], "A-02", "surface", 31.0, 122.09, 10.0, 2
        )
        self.assertEqual(1, len(second["merges"]))
        merge = second["merges"][0]
        merged = second["probability_area"]
        self.assertEqual(merged["id"], merge["merged_area_id"])
        self.assertEqual("planned", merged["status"])
        # 合并圆覆盖两个源概率圆
        for src in (first["probability_area"],):
            gap = haversine_km(merged["center_lat"], merged["center_lon"], src["center_lat"], src["center_lon"])
            self.assertLessEqual(gap + src["radius_km"], merged["radius_km"] + 0.05)
        # 两个源概率区均转为已合并并指向合并结果
        state = self.service.state()
        by_id = {a["id"]: a for a in state["search_areas"]}
        for src in (first["probability_area"], second["probability_area"]):
            if src["id"] == merged["id"]:
                continue
            self.assertEqual("merged", by_id[src["id"]]["status"])
            self.assertEqual(merged["id"], by_id[src["id"]]["merged_into_id"])
        # 合并关系持久化
        self.assertEqual(1, len(state["area_merges"]))
        record = state["area_merges"][0]
        self.assertEqual(merged["id"], record["merged_area_id"])
        self.assertEqual(2, len(record["source_codes"]))
        # 已合并区域不能分配
        asset = self.service.list_assets()[0]
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_area("coord1", "coordinator", first["probability_area"]["id"], asset["id"])
        self.assertEqual(409, ctx.exception.status)

    def test_different_kind_does_not_merge(self):
        incident = self._incident(drift_speed=0.0)
        self._asset()
        self.service.create_search_area(
            "coord1", "coordinator", incident["id"], "A-01", "surface", 31.0, 122.0, 10.0, 1
        )
        air = self.service.create_search_area(
            "coord1", "coordinator", incident["id"], "A-02", "air", 31.0, 122.05, 10.0, 1
        )
        self.assertEqual([], air["merges"])
        # 没有空中能力资源 → 待放行，原因写明没有具备能力的资源
        self.assertEqual("pending_release", air["probability_area"]["status"])
        self.assertIn("没有具备", air["probability_area"]["gate_reason"])

    def test_pending_release_records_unreachable_assets(self):
        incident = self._incident(drift_speed=0.0)
        self._asset(range_km=20.0)
        result = self.service.create_search_area(
            "coord1", "coordinator", incident["id"], "A-FAR", "surface", 31.5, 122.5, 5.0, 3
        )
        prob = result["probability_area"]
        self.assertEqual("pending_release", prob["status"])
        self.assertIn("海巡01", prob["gate_reason"])
        self.assertIn("待放行", prob["gate_reason"])
        self.assertFalse(result["gate"]["reachable"])
        detail = result["gate"]["details"][0]
        self.assertEqual("海巡01", detail["asset_name"])
        self.assertFalse(detail["reachable"])
        self.assertGreater(detail["shortfall_km"], 0)
        # 待放行区域不能分配，且错误说明哪条资源够不着
        asset = self.service.list_assets()[0]
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_area("coord1", "coordinator", prob["id"], asset["id"])
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("海巡01", str(ctx.exception))
        # 待放行区域未结束前事件不能关闭
        current = next(i for i in self.service.state()["incidents"] if i["id"] == incident["id"])
        with self.assertRaises(DomainError) as ctx2:
            self.service.close_incident("coord1", "coordinator", incident["id"], "resolved", current["version"])
        self.assertEqual(409, ctx2.exception.status)

    def test_manual_review_area_cannot_be_assigned(self):
        incident = self._incident()
        self._asset()
        result = self.service.create_search_area(
            "coord1", "coordinator", incident["id"], "A-01", "surface", 31.0, 122.0, 10.0, 1
        )
        asset = self.service.list_assets()[0]
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_area("coord1", "coordinator", result["saved_area"]["id"], asset["id"])
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("待复核", str(ctx.exception))

    def test_projection_merge_and_gate_survive_reopen(self):
        incident = self._incident(drift_speed=0.0)
        self._asset()
        self.service.create_search_area(
            "coord1", "coordinator", incident["id"], "A-01", "surface", 31.0, 122.0, 10.0, 1
        )
        second = self.service.create_search_area(
            "coord1", "coordinator", incident["id"], "A-02", "surface", 31.0, 122.09, 10.0, 2
        )
        merged_id = second["probability_area"]["id"]
        # 模拟页面重开：用同一数据库重新构建服务
        reopened = MaritimeSARService(self.db_path)
        state = reopened.state()
        merged = next(a for a in state["search_areas"] if a["id"] == merged_id)
        self.assertEqual("probability", merged["area_role"])
        self.assertEqual("planned", merged["status"])
        self.assertTrue(merged["gate_checks"])
        self.assertTrue(all(g["reachable"] for g in merged["gate_checks"]))
        self.assertEqual(1, len(state["area_merges"]))
        self.assertEqual(merged_id, state["area_merges"][0]["merged_area_id"])
        manuals = [a for a in state["search_areas"] if a["area_role"] == "manual"]
        self.assertEqual(2, len(manuals))
        self.assertTrue(all(a["status"] == "review" for a in manuals))


if __name__ == "__main__":
    unittest.main()
