import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store
import rotation as rot

T0, T1, T2, T3 = ("2026-09-27T08:00:00Z", "2026-09-27T10:00:00Z",
                  "2026-09-27T12:00:00Z", "2026-09-27T14:00:00Z")


class RuleTest(unittest.TestCase):
    universe = [
        {"id": 1, "name": "医院", "priority": 1, "power_mw": 50},
        {"id": 2, "name": "水厂", "priority": 2, "power_mw": 40},
        {"id": 3, "name": "通信", "priority": 2, "power_mw": 30},
        {"id": 4, "name": "冷链", "priority": 3, "power_mw": 45},
        {"id": 5, "name": "商场", "priority": 3, "power_mw": 25},
    ]

    def test_feasible_manual_batches_and_segments(self):
        batches = [
            {"batch_no": 1, "start_time": T0, "end_time": T2, "facility_ids": [2]},
            {"batch_no": 2, "start_time": T2, "end_time": T3, "facility_ids": [4]},
        ]
        r = rot.evaluate(self.universe, 120, T0, T3, batches)
        self.assertTrue(r["feasible"], r["conflicts"])
        self.assertEqual(50, r["reserved_mw"])
        self.assertEqual([30, 25], [s["margin_mw"] for s in r["segments"]])
        # 未进入批次的二级用户计入全程停供提示
        self.assertEqual({3, 5}, {u["facility_id"] for u in r["unscheduled"]})

    def test_overlapping_batches_union_load_caught(self):
        # 两批重叠：50 + 40 + 45 = 135 > 120
        batches = [
            {"batch_no": 1, "start_time": T0, "end_time": T2, "facility_ids": [2]},
            {"batch_no": 2, "start_time": T1, "end_time": T3, "facility_ids": [4]},
        ]
        r = rot.evaluate(self.universe, 120, T0, T3, batches)
        self.assertFalse(r["feasible"])
        self.assertIn("capacity_overload", [c["code"] for c in r["conflicts"]])
        self.assertEqual({2, 4}, set(r["pending_facility_ids"]))

    def test_same_facility_overlapping_slot_rejected(self):
        batches = [
            {"batch_no": 1, "start_time": T0, "end_time": T2, "facility_ids": [2, 3]},
            {"batch_no": 2, "start_time": T1, "end_time": T3, "facility_ids": [2, 4]},
        ]
        r = rot.evaluate(self.universe, 200, T0, T3, batches)
        codes = [c["code"] for c in r["conflicts"]]
        self.assertIn("facility_overlap", codes)
        self.assertIn(2, [c["facility_ids"][0] for c in r["conflicts"] if c["code"] == "facility_overlap"])

    def test_priority1_always_on_and_cannot_rotate(self):
        r = rot.evaluate(self.universe, 120, T0, T3,
                         [{"batch_no": 1, "start_time": T0, "end_time": T1, "facility_ids": [1]}])
        self.assertFalse(r["feasible"])
        self.assertIn("always_on_in_batch", [c["code"] for c in r["conflicts"]])
        short = rot.evaluate(self.universe, 40, T0, T3, [])
        self.assertIn("always_on_over_capacity", [c["code"] for c in short["conflicts"]])

    def test_auto_build_rotates_fairly(self):
        r = rot.build_batches(self.universe, 120, T0, T3, slot_minutes=120)
        self.assertTrue(r["feasible"], r["conflicts"])
        self.assertEqual(3, len(r["rotation_table"]))
        appear = {}
        first_slot = {f["id"] for f in r["rotation_table"][0]["facilities"]}
        for b in r["rotation_table"]:
            for f in b["facilities"]:
                appear[f["id"]] = appear.get(f["id"], 0) + 1
        # 首批二级先于三级获得电量，且可容纳的二三级用户都至少轮到一次
        self.assertEqual({2, 3}, first_slot)
        for fid in (2, 3, 4, 5):
            self.assertTrue(appear.get(fid, 0) >= 1, f"用户 {fid} 未轮到")
        # 每个槽一级保留 + 轮供不超过容量
        self.assertTrue(all(s["margin_mw"] >= 0 for s in r["segments"]))

    def test_auto_build_single_unfit_facility_conflict(self):
        universe = [self.universe[0], {"id": 9, "name": "大钢厂", "priority": 3, "power_mw": 80}]
        r = rot.build_batches(universe, 120, T0, T3, slot_minutes=120)
        self.assertFalse(r["feasible"])
        self.assertIn("facility_unfit", [c["code"] for c in r["conflicts"]])
        self.assertIn(9, r["pending_facility_ids"])

    def test_fingerprint_changes_with_power(self):
        fp1 = rot.facilities_fingerprint(self.universe)
        changed = [dict(f, power_mw=41) if f["id"] == 2 else f for f in self.universe]
        self.assertNotEqual(fp1, rot.facilities_fingerprint(changed))
        reasons = rot.basis_reasons(fp1, 1, changed, 2)
        self.assertEqual(2, len(reasons))


class RotationServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.sub = self.s.register_asset("d", "dispatcher", "SUB", "中心站", "substation", 200, "城区")
        self.f1 = self.s.register_facility("d", "dispatcher", "医院", "hospital", self.sub["id"], 1, 50)
        self.f2 = self.s.register_facility("d", "dispatcher", "水厂", "water", self.sub["id"], 2, 40)
        self.f3 = self.s.register_facility("d", "dispatcher", "冷链", "coldchain", self.sub["id"], 3, 45)
        self.outage = self.s.create_outage("d", "dispatcher", "OUT-9", "全站停电", ["城区"])

    def tearDown(self):
        self.s.store.close()
        self.tmp.cleanup()

    def body(self, **over):
        base = {"outage_id": self.outage["id"], "capacity_mw": 120,
                "start_time": T0, "end_time": T3, "slot_minutes": 120}
        base.update(over)
        return base

    def test_conflicting_candidate_is_not_persisted(self):
        bad = [{"batch_no": 1, "start_time": T0, "end_time": T2, "facility_ids": [self.f2["id"]]},
               {"batch_no": 2, "start_time": T1, "end_time": T3, "facility_ids": [self.f3["id"]]}]
        with self.assertRaises(ApiError) as cm:
            self.s.create_rotation_schedule("d", "dispatcher", self.body(batches=bad))
        self.assertEqual(409, cm.exception.status)
        self.assertTrue(cm.exception.extra["candidate"]["conflicts"])
        self.assertEqual([], self.s.rotation_summaries())
        # 预览对任何身份可用且不落库
        pv = self.s.preview_rotation("f", "field", self.body(batches=bad))
        self.assertFalse(pv["feasible"])
        self.assertEqual([], self.s.rotation_summaries())

    def test_create_supersedes_old_and_supply_flow(self):
        s1 = self.s.create_rotation_schedule("d", "dispatcher", self.body())
        self.assertEqual("active", s1["state"])
        # 一级用户初始即在供
        self.assertEqual("on", next(x["status"] for x in s1["supply"] if x["facility_id"] == self.f1["id"]))
        s2 = self.s.create_rotation_schedule("d", "dispatcher", self.body(capacity_mw=130))
        self.assertEqual("active", s2["state"])
        self.assertEqual("superseded", self.s.rotation_detail(s1["id"])["state"])
        # 被取代的安排不能再现场操作
        with self.assertRaises(ApiError):
            self.s.set_supply("f", "field", {"schedule_id": s1["id"], "facility_id": self.f2["id"], "status": "on"})
        # 现场接通二三级用户后在供清单和实际余量更新
        d = self.s.set_supply("f", "field", {"schedule_id": s2["id"], "facility_id": self.f2["id"], "status": "on", "note": "已合闸"})
        row = next(x for x in d["supply"] if x["facility_id"] == self.f2["id"])
        self.assertEqual("on", row["status"])
        self.assertEqual(90, d["actual_load_mw"])
        self.assertEqual(40, d["live_margin_mw"])

    def test_unscheduled_facility_cannot_be_energized(self):
        sched = self.s.create_rotation_schedule("d", "dispatcher", self.body(
            batches=[{"batch_no": 1, "start_time": T0, "end_time": T3, "facility_ids": [self.f2["id"]]}]))
        with self.assertRaises(ApiError):
            self.s.set_supply("f", "field", {"schedule_id": sched["id"], "facility_id": self.f3["id"], "status": "on"})

    def test_power_change_invalidates_schedule(self):
        sched = self.s.create_rotation_schedule("d", "dispatcher", self.body())
        self.s.update_facility_power("d", "dispatcher", self.f2["id"], 90)
        # 汇总中呈现 stale 及原因
        summary = next(x for x in self.s.rotation_summaries() if x["id"] == sched["id"])
        self.assertEqual("stale", summary["state"])
        self.assertTrue(summary["invalid_reasons"])
        # 再次现场操作时落为 invalidated 并被拒绝
        with self.assertRaises(ApiError) as cm:
            self.s.set_supply("f", "field", {"schedule_id": sched["id"], "facility_id": self.f2["id"], "status": "on"})
        self.assertEqual(409, cm.exception.status)
        self.assertEqual("invalidated", self.s.rotation_detail(sched["id"])["state"])

    def test_plan_version_change_invalidates_schedule(self):
        sched = self.s.create_rotation_schedule("d", "dispatcher", self.body())
        # 编制恢复计划即产生 v1
        plan = self.s.create_plan("d", "dispatcher", self.outage["id"], [
            {"seq": 1, "action": "送电", "asset": "SUB", "required_mw": 80}])
        self.assertEqual(1, plan["version"])
        summary = next(x for x in self.s.rotation_summaries() if x["id"] == sched["id"])
        self.assertEqual("stale", summary["state"])
        self.assertTrue(any("计划版本" in r for r in summary["invalid_reasons"]))
        with self.assertRaises(ApiError):
            self.s.set_supply("f", "field", {"schedule_id": sched["id"], "facility_id": self.f2["id"], "status": "on"})

    def test_permissions(self):
        with self.assertRaises(ApiError):
            self.s.create_rotation_schedule("f", "field", self.body())
        with self.assertRaises(ApiError):
            self.s.update_facility_power("f", "field", self.f2["id"], 60)


if __name__ == "__main__":
    unittest.main()
