"""轮换保供调度规则。

纯函数模块：不接触数据库与 HTTP。输入重要用户清单、备用容量和起止时间，
输出候选轮换安排；容量不足时给出冲突与待调整用户，是否写入由服务层决定。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

EPS = 1e-9


def parse_instant(value: str) -> float:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"时间格式无效：{value}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def format_instant(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def facilities_fingerprint(facilities: list[dict]) -> str:
    """重要用户清单指纹：用户、优先级或功率任一变化都会改变指纹，使旧安排失效。"""
    payload = [
        [int(f["id"]), int(f["priority"]), round(float(f["backup_power_mw"]), 6)]
        for f in sorted(facilities, key=lambda item: int(item["id"]))
    ]
    return hashlib.sha1(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def windows_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return parse_instant(a_start) < parse_instant(b_end) and parse_instant(b_start) < parse_instant(a_end)


def find_user_overlaps(assignments: list[dict]) -> list[int]:
    """返回落在重叠时段的用户 id：同一用户两条安排的时段相交即为违规。"""
    bad: set[int] = set()
    by_user: dict[int, list[dict]] = {}
    for item in assignments:
        by_user.setdefault(int(item["facility_id"]), []).append(item)
    for facility_id, slots in by_user.items():
        for i in range(len(slots)):
            for k in range(i + 1, len(slots)):
                if windows_overlap(slots[i]["slot_start"], slots[i]["slot_end"], slots[k]["slot_start"], slots[k]["slot_end"]):
                    bad.add(facility_id)
    return sorted(bad)


def peak_load_mw(assignments: list[dict]) -> float:
    """最大同时负荷：一级基线 + 各轮换轮次中最大的同时功率。"""
    baseline = sum(float(a["allocated_mw"]) for a in assignments if int(a["round_no"]) == 0)
    rounds: dict[int, float] = {}
    for a in assignments:
        no = int(a["round_no"])
        if no:
            rounds[no] = rounds.get(no, 0.0) + float(a["allocated_mw"])
    return baseline + max(rounds.values(), default=0.0)


def margin_mw(assignments: list[dict], capacity_mw: float) -> float:
    """余量 = 备用容量 - 最大同时负荷。"""
    return round(float(capacity_mw) - peak_load_mw(assignments), 6)


def schedule_rotation(facilities: list[dict], capacity_mw: float, start_at: str, end_at: str) -> dict:
    """生成候选轮换安排（纯计算，不写入）。

    规则：一级用户全程保留；二三级按优先级、功率从大到小装入轮换轮次（先装先排）；
    同一用户只出现在一个时段；任何时刻同时负荷不超过备用容量。
    容量不足时不报错而是给出 conflicts 与 pending，由调用方决定是否采用。
    """
    start_ts, end_ts = parse_instant(start_at), parse_instant(end_at)
    if end_ts <= start_ts:
        raise ValueError("结束时间必须晚于开始时间")
    try:
        capacity = float(capacity_mw)
    except (TypeError, ValueError) as exc:
        raise ValueError("备用容量必须为数字") from exc
    if capacity <= 0:
        raise ValueError("备用容量必须为正数")
    start_at, end_at = format_instant(start_ts), format_instant(end_ts)

    def power_of(f: dict) -> float: return float(f["backup_power_mw"])
    def name_of(f: dict) -> str: return str(f.get("name", f["id"]))

    assignments: list[dict] = []
    conflicts: list[str] = []
    pending: list[dict] = []

    # 一级用户：全程保留，备用容量装不下的列入待调整
    kept_baseline, p1_total, p1_pending = 0.0, 0.0, 0
    for f in sorted((f for f in facilities if int(f["priority"]) == 1), key=lambda f: int(f["id"])):
        power = power_of(f)
        p1_total += power
        if kept_baseline + power <= capacity + EPS:
            kept_baseline += power
            assignments.append({"facility_id": int(f["id"]), "name": name_of(f), "priority": 1,
                                "allocated_mw": power, "slot_start": start_at, "slot_end": end_at, "round_no": 0})
        else:
            p1_pending += 1
            pending.append({"facility_id": int(f["id"]), "name": name_of(f),
                            "reason": "一级用户需全程保供，剩余备用容量不足"})
    if p1_pending:
        conflicts.append(f"一级用户保供功率合计 {p1_total:g}MW，超过备用容量 {capacity:g}MW")

    available = capacity - kept_baseline

    # 二三级用户：按功率轮流，单户超过可轮换容量的列入待调整
    rotating = sorted((f for f in facilities if int(f["priority"]) in (2, 3)),
                      key=lambda f: (int(f["priority"]), -power_of(f), int(f["id"])))
    placeable = []
    for f in rotating:
        if power_of(f) <= available + EPS:
            placeable.append(f)
        else:
            pending.append({"facility_id": int(f["id"]), "name": name_of(f),
                            "reason": f"单户功率 {power_of(f):g}MW 超过可轮换容量 {max(available, 0):g}MW"})
    overflow = len(pending) - p1_pending
    if overflow:
        conflicts.append(f"{overflow} 个二三级用户功率超过可轮换容量 {max(available, 0):g}MW，待调整")

    # 全部同时装得下则共用全程；否则先装先排分成若干轮，每轮平分时段
    rounds: list[dict] = []
    if placeable:
        total = sum(power_of(f) for f in placeable)
        if total <= available + EPS:
            rounds = [{"load": total, "members": list(placeable)}]
        else:
            for f in placeable:
                for r in rounds:
                    if r["load"] + power_of(f) <= available + EPS:
                        r["members"].append(f)
                        r["load"] += power_of(f)
                        break
                else:
                    rounds.append({"load": power_of(f), "members": [f]})
    for index, r in enumerate(rounds):
        slot = (end_ts - start_ts) / len(rounds)
        slot_start, slot_end = format_instant(start_ts + index * slot), format_instant(start_ts + (index + 1) * slot)
        for f in r["members"]:
            assignments.append({"facility_id": int(f["id"]), "name": name_of(f), "priority": int(f["priority"]),
                                "allocated_mw": power_of(f), "slot_start": slot_start, "slot_end": slot_end,
                                "round_no": index + 1})

    return {"capacity_mw": capacity, "start_at": start_at, "end_at": end_at,
            "baseline_mw": round(kept_baseline, 6), "rotating_available_mw": round(available, 6),
            "rounds": len(rounds), "assignments": assignments,
            "conflicts": conflicts, "pending": pending,
            "margin_mw": margin_mw(assignments, capacity)}
