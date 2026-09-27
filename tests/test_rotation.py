import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rotation_rules as rules
from app import ApiError, GridService, Store

START, END = "2026-09-27T08:00:00Z", "2026-09-27T20:00:00Z"


class RotationRulesTest(unittest.TestCase):
    def test_fingerprint_tracks_power_and_priority(self):
        facilities = [{"id": 1, "priority": 1, "backup_power_mw": 30}, {"id": 2, "priority": 2, "backup_power_mw": 20}]
        base = rules.facilities_fingerprint(facilities)
        self.assertEqual(base, rules.facilities_fingerprint(list(reversed(facilities))))
        changed = [dict(f) for f in facilities]; changed[0]["backup_power_mw"] = 31
        self.assertNotEqual(base, rules.facilities_fingerprint(changed))

    def test_find_user_overlaps(self):
        good = [{"facility_id": 1, "slot_start": "2026-09-27T08:00:00Z", "slot_end": "2026-09-27T12:00:00Z"},
                {"facility_id": 1, "slot_start": "2026-09-27T12:00:00Z", "slot_end": "2026-09-27T20:00:00Z"}]
        self.assertEqual([], rules.find_user_overlaps(good))
        bad = good + [{"facility_id": 1, "slot_start": "2026-09-27T10:00:00Z", "slot_end": "2026-09-27T13:00:00Z"}]
        self.assertEqual([1], rules.find_user_overlaps(bad))

    def test_window_and_capacity_validation(self):
        with self.assertRaises(ValueError):
            rules.schedule_rotation([], 60, END, START)
        with self.assertRaises(ValueError):
            rules.schedule_rotation([], 0, START, END)
        with self.assertRaises(ValueError):
            rules.schedule_rotation([], 60, "not-a-time", END)


class RotationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = GridService(Store(Path(self.tmp.name) / "r.db"))
        self.sub = self.s.register_asset("d", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.hospital = self.s.register_facility("d", "dispatcher", "市医院", "hospital", self.sub["id"], 1, 30)
        self.school = self.s.register_facility("d", "dispatcher", "学校", "school", self.sub["id"], 2, 20)
        self.mall = self.s.register_facility("d", "dispatcher", "商场", "mall", self.sub["id"], 3, 25)
        self.hotel = self.s.register_facility("d", "dispatcher", "酒店", "hotel", self.sub["id"], 3, 15)
        self.outage = self.s.create_outage("d", "dispatcher", "OUT-R", "主变故障", ["A"])

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def commit(self, cap=60, start=START, end=END):
        return self.s.rotation.commit_batch("d", "dispatcher", self.outage["id"], start, end, cap)

    def view(self):
        return self.s.rotation.rotation_view(self.outage["id"])

    def test_commit_rotates_and_keeps_priority1_full_window(self):
        batch = self.commit(60)
        self.assertEqual("active", batch["state"])
        p1 = [a for a in batch["assignments"] if a["facility_id"] == self.hospital["id"]]
        self.assertEqual(1, len(p1))
        self.assertEqual(0, p1[0]["round_no"])
        self.assertEqual((START, END), (p1[0]["slot_start"], p1[0]["slot_end"]))
        # 二三级按功率分轮：学校20 第一轮，商场25 第二轮，酒店15 第三轮（可轮换容量30）
        rounds = {a["round_no"]: a["facility_id"] for a in batch["assignments"] if a["round_no"]}
        self.assertEqual({1: self.school["id"], 2: self.mall["id"], 3: self.hotel["id"]}, rounds)
        # 同一用户不落在重叠时段，余量 = 容量 - 最大同时负荷
        self.assertEqual([], rules.find_user_overlaps(batch["assignments"]))
        self.assertEqual(5, batch["margin_mw"])
        self.assertGreaterEqual(batch["margin_mw"], 0)

    def test_preview_never_writes(self):
        cand = self.s.rotation.preview_batch("d", "dispatcher", self.outage["id"], START, END, 60)
        self.assertFalse(cand["written"])
        self.assertEqual(3, cand["rounds"])
        self.assertEqual([], rules.find_user_overlaps(cand["assignments"]))
        self.assertEqual([], self.view()["batches"])

    def test_shortage_reports_conflicts_and_pending_without_writing(self):
        cand = self.s.rotation.preview_batch("d", "dispatcher", self.outage["id"], START, END, 20)
        self.assertTrue(cand["conflicts"])
        self.assertIn(self.hospital["id"], [p["facility_id"] for p in cand["pending"]])
        with self.assertRaises(ApiError) as ctx:
            self.commit(20)
        self.assertEqual(409, ctx.exception.status)
        self.assertTrue(ctx.exception.extra["conflicts"])
        self.assertEqual([], self.view()["batches"])

    def test_single_user_exceeding_rotating_capacity_is_pending(self):
        cand = self.s.rotation.preview_batch("d", "dispatcher", self.outage["id"], START, END, 40)
        self.assertEqual({self.school["id"], self.mall["id"], self.hotel["id"]}, {p["facility_id"] for p in cand["pending"]})
        with self.assertRaises(ApiError):
            self.commit(40)

    def test_supply_events_update_in_supply_list(self):
        batch = self.commit(60)
        bid = batch["id"]
        res = self.s.rotation.record_supply("f", "field", bid, self.hospital["id"], "connected")
        self.assertEqual("connected", res["state"])
        self.assertEqual([self.hospital["id"]], [s["facility_id"] for s in self.view()["supply"]])
        with self.assertRaises(ApiError):  # 重复接通
            self.s.rotation.record_supply("f", "field", bid, self.hospital["id"], "connected")
        with self.assertRaises(ApiError):  # 未接通不能停供
            self.s.rotation.record_supply("f", "field", bid, self.school["id"], "disconnected")
        self.s.rotation.record_supply("f", "field", bid, self.hospital["id"], "disconnected")
        self.assertEqual([], self.view()["supply"])
        with self.assertRaises(ApiError):  # 用户不在批次中
            self.s.rotation.record_supply("f", "field", bid, 999, "connected")
        with self.assertRaises(ApiError):  # 角色无权
            self.s.rotation.record_supply("o", "operator", bid, self.school["id"], "connected")

    def test_power_change_invalidates_old_batch(self):
        batch = self.commit(60)
        self.s.update_facility_power("d", "dispatcher", self.mall["id"], 40)
        stale = self.view()["batches"][0]
        self.assertEqual("stale", stale["state"])
        self.assertEqual("重要用户功率或清单已变化", stale["invalid_reason"])
        with self.assertRaises(ApiError):  # 失效批次不能更新在供
            self.s.rotation.record_supply("f", "field", batch["id"], self.hospital["id"], "connected")

    def test_new_facility_invalidates_old_batch(self):
        self.commit(60)
        self.s.register_facility("d", "dispatcher", "新医院", "hospital", self.sub["id"], 1, 10)
        self.assertEqual("stale", self.view()["batches"][0]["state"])

    def test_plan_version_change_invalidates_old_batch(self):
        self.commit(60)
        plan = self.s.create_plan("d", "dispatcher", self.outage["id"], [{"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 50}])
        plan = self.s.submit_plan("d", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("d", "dispatcher", plan["id"], plan["revision"])
        self.s.activate_plan("d", "dispatcher", plan["id"], plan["revision"])
        stale = self.view()["batches"][0]
        self.assertEqual("stale", stale["state"])
        self.assertEqual("计划版本已变化", stale["invalid_reason"])

    def test_overlapping_commit_supersedes_old_batch(self):
        b1 = self.commit(60, "2026-09-27T08:00:00Z", "2026-09-27T12:00:00Z")
        b2 = self.commit(60, "2026-09-27T12:00:00Z", "2026-09-27T20:00:00Z")  # 相接不重叠，两批次都在效
        states = {b["id"]: b["state"] for b in self.view()["batches"]}
        self.assertEqual("active", states[b1["id"]])
        self.assertEqual("active", states[b2["id"]])
        b3 = self.commit(60, "2026-09-27T10:00:00Z", "2026-09-27T14:00:00Z")  # 与 b1、b2 均重叠
        view = self.view()
        states = {b["id"]: b["state"] for b in view["batches"]}
        self.assertEqual("stale", states[b1["id"]])
        self.assertEqual("stale", states[b2["id"]])
        self.assertEqual("active", states[b3["id"]])
        # 在效批次中同一用户不落在重叠时段
        active = [a for b in view["batches"] if b["state"] == "active" for a in b["assignments"]]
        self.assertEqual([], rules.find_user_overlaps(active))

    def test_permissions_and_window_validation(self):
        with self.assertRaises(ApiError):
            self.s.rotation.commit_batch("o", "operator", self.outage["id"], START, END, 60)
        with self.assertRaises(ApiError):
            self.s.rotation.commit_batch(None, "dispatcher", self.outage["id"], START, END, 60)
        with self.assertRaises(ApiError):
            self.commit(60, END, START)
        with self.assertRaises(ApiError):
            self.commit(0)


if __name__ == "__main__": unittest.main()
