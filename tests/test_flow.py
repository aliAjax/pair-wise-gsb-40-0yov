import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, MaritimeSARService  # noqa: E402
from drift import distance_km  # noqa: E402


class MaritimeSARFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = MaritimeSARService(Path(self.tmp.name) / "test.db")
        self.incident = self.service.create_incident(
            "coord1", "coordinator", "SAR-001", "海燕号", 31.0, 122.0, 10.0, 3, "东海中心"
        )
        self.asset = self.service.add_asset(
            "coord1", "coordinator", "海巡01", "vessel", ["surface", "night"], 31.0, 122.0, 20, 100, 5
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_complete_assignment_clue_offline_and_close_flow(self):
        area = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-01", "surface", 31.1, 122.1, 8, 1
        )
        assigned = self.service.assign_area("coord1", "coordinator", area["id"], self.asset["id"], self.asset["version"])
        self.assertEqual("assigned", assigned["status"])
        clue = self.service.record_clue(
            "field1", "field", self.incident["id"], "evt-1", 31.1, 122.1, 0.9, "visual", area["id"]
        )
        self.assertEqual("verified", self.service.verify_clue("analyst1", "analyst", clue["id"], "verified")["status"])
        batch = self.service.merge_offline_batch(
            "field1", "field", "batch-1",
            [{"type": "clue", "client_event_id": "off-1", "incident_id": self.incident["id"],
              "latitude": 31.11, "longitude": 122.11, "confidence": 0.7, "source": "radio"}],
        )
        self.assertEqual(1, batch["summary"]["accepted"])
        self.assertTrue(self.service.merge_offline_batch("field1", "field", "batch-1", [])["idempotent"])
        updated_asset = self.service.list_assets()[0]
        self.service.withdraw_asset("coord1", "coordinator", self.asset["id"], "任务移交", updated_asset["version"])
        # 保存区域时自动生成的概率区域也要一并结束后才能关闭事件
        for a in self.service.state()["search_areas"]:
            if a["status"] in ("planned", "assigned", "active", "pending_release"):
                self.service.complete_area("coord1", "coordinator", a["id"], "abandoned", a["version"])
        current_incident = [x for x in self.service.state()["incidents"] if x["id"] == self.incident["id"]][0]
        closed = self.service.close_incident("coord1", "coordinator", self.incident["id"], "resolved", current_incident["version"])
        self.assertEqual("closed", closed["status"])
        self.assertGreaterEqual(len(self.service.incident_timeline(self.incident["id"])), 6)

    def test_duplicate_alarm_and_invalid_position_are_controlled(self):
        duplicate = self.service.create_incident(
            "op1", "operator", "SAR-002", "海燕号", 31.01, 122.01, 5.0, 3, "东海中心"
        )
        self.assertEqual("duplicate", duplicate["status"])
        self.assertEqual(self.incident["id"], duplicate["duplicate_of"])
        invalid = self.service.record_clue(
            "field1", "field", self.incident["id"], "evt-far", 45.0, 130.0, 0.8, "radio"
        )
        self.assertEqual("invalid", invalid["status"])
        with self.assertRaises(DomainError):
            self.service.verify_clue("field1", "field", invalid["id"], "verified")

    def test_assignment_conflict_and_permission(self):
        area = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-02", "surface", 31.1, 122.1, 5
        )
        self.service.assign_area("coord1", "coordinator", area["id"], self.asset["id"], self.asset["version"])
        area2 = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-03", "surface", 31.2, 122.2, 5
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_area("coord1", "coordinator", area2["id"], self.asset["id"], self.asset["version"])
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.create_search_area("field1", "field", self.incident["id"], "A-04", "surface", 31, 122, 5)
        self.assertEqual(403, ctx2.exception.status)


class DriftDispatchTest(unittest.TestCase):
    """漂移推算调度：概率区域生成、合并、待复核、待放行与持久化。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = MaritimeSARService(self.db_path)
        self.incident = self.service.create_incident(
            "coord1", "coordinator", "SAR-100", "远渔号", 31.0, 122.0, 10.0, 3, "东海中心",
            drift_direction=90.0, drift_speed_kn=5.0,
        )
        self.asset = self.service.add_asset(
            "coord1", "coordinator", "海巡01", "vessel", ["surface"], 31.0, 122.0, 20, 400, 5
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _age_incident(self, hours):
        """把事件发生时间往前拨，模拟目标已经漂了 hours 小时。"""
        with self.service.connect() as conn:
            row = conn.execute("SELECT created_at FROM incidents WHERE id=?", (self.incident["id"],)).fetchone()
            start = datetime.fromisoformat(row["created_at"]) - timedelta(hours=hours)
            conn.execute("UPDATE incidents SET created_at=? WHERE id=?",
                         (start.isoformat(timespec="seconds"), self.incident["id"]))

    def _drift_areas(self):
        return [a for a in self.service.state()["search_areas"] if a["origin"] == "drift"]

    def test_projection_on_area_save(self):
        self._age_incident(2.0)  # 5kn * 2h = 10nm = 18.52km 向东
        area = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-01", "surface", 31.0, 122.0, 8, 1
        )
        drift_areas = self._drift_areas()
        self.assertEqual(1, len(drift_areas))
        proj = drift_areas[0]
        self.assertEqual("planned", proj["status"])
        self.assertEqual(area["id"], proj["source_area_id"])
        self.assertAlmostEqual(2.0, proj["elapsed_hours"], delta=0.05)
        moved = distance_km(31.0, 122.0, proj["center_lat"], proj["center_lon"])
        self.assertAlmostEqual(18.52, moved, delta=0.5)
        self.assertGreater(proj["center_lon"], 122.0)  # 向正东漂移
        self.assertGreater(proj["radius_km"], 10.0)  # 半径随经过时长放大
        actions = [t["action"] for t in self.service.incident_timeline(self.incident["id"])]
        self.assertIn("area.projected", actions)

    def test_overlapping_projection_merges_into_one(self):
        self._age_incident(1.0)
        self.service.create_search_area("coord1", "coordinator", self.incident["id"], "A-01", "surface", 31.0, 122.0, 8, 1)
        proj2 = self.service.reproject_incident("coord1", "coordinator", self.incident["id"])
        first, second = self._drift_areas()
        self.assertEqual("merged", first["status"])
        self.assertEqual(proj2["id"], first["merged_into"])
        self.assertEqual(second["id"], proj2["id"])
        self.assertEqual("planned", second["status"])
        self.assertGreaterEqual(second["radius_km"], first["radius_km"])
        actions = [t["action"] for t in self.service.incident_timeline(self.incident["id"])]
        self.assertIn("area.merged", actions)

    def test_non_overlapping_projection_sends_old_area_to_review(self):
        self._age_incident(3.0)
        self.service.create_search_area("coord1", "coordinator", self.incident["id"], "A-01", "surface", 31.0, 122.0, 8, 1)
        # 漂移方向调转 180°，新旧概率圆不再重叠
        with self.service.connect() as conn:
            conn.execute("UPDATE incidents SET drift_direction=270.0 WHERE id=?", (self.incident["id"],))
        self.service.reproject_incident("coord1", "coordinator", self.incident["id"])
        first, second = self._drift_areas()
        self.assertEqual("pending_review", first["status"])
        self.assertIsNone(first["merged_into"])
        self.assertEqual("planned", second["status"])
        actions = [t["action"] for t in self.service.incident_timeline(self.incident["id"])]
        self.assertIn("area.superseded", actions)

    def test_out_of_range_area_is_held_then_released(self):
        self.service.add_asset("coord1", "coordinator", "小艇", "vessel", ["surface"], 31.0, 122.0, 15, 30, 5)
        self._age_incident(5.0)  # 漂移约 46km，小艇航程 30km 够不着
        self.service.create_search_area("coord1", "coordinator", self.incident["id"], "A-01", "surface", 31.0, 122.0, 8, 1)
        proj = self._drift_areas()[0]
        self.assertEqual("pending_release", proj["status"])
        self.assertIn("小艇", proj["hold_reason"])
        self.assertNotIn("海巡01", proj["hold_reason"])
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_area("coord1", "coordinator", proj["id"], self.asset["id"], self.asset["version"])
        self.assertEqual(409, ctx.exception.status)
        released = self.service.release_area("coord1", "coordinator", proj["id"], note="确认由海巡01远程执行")
        self.assertEqual("planned", released["status"])
        assigned = self.service.assign_area("coord1", "coordinator", proj["id"], self.asset["id"], self.asset["version"])
        self.assertEqual("assigned", assigned["status"])
        actions = [t["action"] for t in self.service.incident_timeline(self.incident["id"])]
        self.assertIn("area.held", actions)
        self.assertIn("area.released", actions)

    def test_pending_release_area_blocks_incident_close(self):
        self.service.add_asset("coord1", "coordinator", "小艇", "vessel", ["surface"], 31.0, 122.0, 15, 30, 5)
        self._age_incident(5.0)
        self.service.create_search_area("coord1", "coordinator", self.incident["id"], "A-01", "surface", 31.0, 122.0, 8, 1)
        current = [x for x in self.service.state()["incidents"] if x["id"] == self.incident["id"]][0]
        with self.assertRaises(DomainError) as ctx:
            self.service.close_incident("coord1", "coordinator", self.incident["id"], "resolved", current["version"])
        self.assertEqual(409, ctx.exception.status)

    def test_projection_merge_and_hold_survive_reopen(self):
        self.service.add_asset("coord1", "coordinator", "小艇", "vessel", ["surface"], 31.0, 122.0, 15, 30, 5)
        self._age_incident(5.0)
        self.service.create_search_area("coord1", "coordinator", self.incident["id"], "A-01", "surface", 31.0, 122.0, 8, 1)
        self.service.reproject_incident("coord1", "coordinator", self.incident["id"])
        reopened = MaritimeSARService(self.db_path)  # 模拟页面重开后重新读取
        drift_areas = [a for a in reopened.state()["search_areas"] if a["origin"] == "drift"]
        self.assertEqual(2, len(drift_areas))
        merged = [a for a in drift_areas if a["status"] == "merged"]
        held = [a for a in drift_areas if a["status"] == "pending_release"]
        self.assertEqual(1, len(merged))
        self.assertEqual(1, len(held))
        self.assertEqual(held[0]["id"], merged[0]["merged_into"])
        self.assertIn("小艇", held[0]["hold_reason"])
        self.assertGreater(held[0]["elapsed_hours"], 4.9)


if __name__ == "__main__":
    unittest.main()
