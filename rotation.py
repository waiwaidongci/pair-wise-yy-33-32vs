#!/usr/bin/env python3
"""轮换保供规则层（纯函数，不依赖 SQLite / HTTP）。

存储层负责持久化，页面层只负责展示；所有排批与校核规则集中在本模块：

- 一级用户全程保留，功率从备用容量中预先扣除；
- 二、三级用户按优先级与功率轮流排入批次；
- 同一用户不能落入互相重叠的批次时段；
- 任一时段在供总负荷（含重叠批次的并集）不得超过备用容量；
- 容量不足时产出冲突与“待调整用户”，由调用方决定是否写入。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DEFAULT_SLOT_MINUTES = 120
EPS = 1e-6
TIME_EPS = timedelta(microseconds=1)


@dataclass(frozen=True)
class Customer:
    id: int
    name: str
    priority: int
    power_mw: float


def parse_ts(value: str) -> datetime:
    """解析 ISO 8601 时间；无时区按 UTC 处理。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间应为 ISO 8601 格式，例如 2026-09-27T08:00:00Z")
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"时间格式不合法：{value}") from exc
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _customers(universe: list[dict]) -> list[Customer]:
    out = []
    for row in universe:
        try:
            out.append(Customer(int(row["id"]), str(row["name"]),
                                int(row["priority"]), float(row["power_mw"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _customer_dicts(customers: list[Customer]) -> list[dict]:
    return [{"id": c.id, "name": c.name, "priority": c.priority, "power_mw": c.power_mw} for c in customers]


def facilities_fingerprint(universe: list[dict]) -> str:
    """用户清单（编号/优先级/功率）指纹，任一字段变化都会导致旧安排失效。"""
    parts = [f"{int(r['id'])}:{int(r['priority'])}:{round(float(r['power_mw']), 3)}" for r in universe]
    return hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]


def basis_reasons(stored_fp: str, stored_plan_version: int,
                  universe: list[dict], current_plan_version: int) -> list[str]:
    """比较编制依据与现状，返回旧安排失效原因；无变化返回空列表。"""
    reasons: list[str] = []
    if facilities_fingerprint(universe) != stored_fp:
        reasons.append("用户功率或优先级已变更，旧轮换安排失效")
    if int(current_plan_version) != int(stored_plan_version):
        reasons.append(f"计划版本已由 v{int(stored_plan_version)} 变为 v{int(current_plan_version)}，旧轮换安排失效")
    return reasons


def evaluate(universe: list[dict], capacity_mw: object, start_s: str, end_s: str,
             batches: list[dict]) -> dict:
    """校核一份候选轮换安排，返回轮换表、分段余量、冲突和待调整用户。

    本函数只做计算，不写任何存储；feasible=False 时调用方不得落库。
    """
    conflicts: list[dict] = []
    pending: set[int] = set()

    def conflict(code: str, reason: str, **extra: object) -> None:
        item = {"code": code, "reason": reason}
        item.update(extra)
        conflicts.append(item)

    try:
        cap = float(capacity_mw)  # type: ignore[arg-type]
        if cap <= 0:
            raise ValueError
    except (TypeError, ValueError):
        conflict("capacity_invalid", "备用容量必须为正数")
        cap = None

    start = end = None
    try:
        start, end = parse_ts(start_s), parse_ts(end_s)
        if start >= end:
            raise ValueError("开始时间必须早于结束时间")
    except ValueError as exc:
        conflict("window_invalid", f"轮换起止时间不合法：{exc}")

    customers = _customers(universe)
    by_id = {c.id: c for c in customers}
    always_on = sorted((c for c in customers if c.priority == 1), key=lambda c: c.id)
    reserved = round(sum(c.power_mw for c in always_on), 6)

    if cap is not None and reserved > cap + EPS:
        conflict("always_on_over_capacity",
                 f"一级用户全程保留负荷 {reserved:g}MW 超过备用容量 {cap:g}MW，缺口 {round(reserved - cap, 3):g}MW，二、三级用户无法安排",
                 facility_ids=[c.id for c in always_on], shortfall_mw=round(reserved - cap, 3))
        pending.update(c.id for c in customers if c.priority in (2, 3))

    parsed: list[dict] = []
    if start is not None:
        for i, raw in enumerate(batches, 1):
            no = int(raw.get("batch_no") or i)
            try:
                ids = [int(x) for x in (raw.get("facility_ids") or [])]
            except (TypeError, ValueError):
                conflict("batch_facility_invalid", f"批次 {no} 的用户编号不合法", batch_no=no)
                continue
            try:
                bs, be = parse_ts(raw["start_time"]), parse_ts(raw["end_time"])
                if bs >= be:
                    raise ValueError
            except (KeyError, ValueError):
                conflict("batch_time_invalid", f"批次 {no} 的起止时间不合法", batch_no=no)
                continue
            if bs < start - TIME_EPS or be > end + TIME_EPS:
                conflict("batch_outside_window",
                         f"批次 {no}（{fmt(bs)}–{fmt(be)}）超出轮换总时段 {fmt(start)}–{fmt(end)}", batch_no=no)
            parsed.append({"batch_no": no, "start": bs, "end": be, "ids": ids})

    # 引用范围外用户、一级用户进轮换批
    for b in parsed:
        for fid in b["ids"]:
            if fid not in by_id:
                conflict("facility_unknown",
                         f"批次 {b['batch_no']} 引用了不在事故影响范围内的用户 {fid}",
                         batch_no=b["batch_no"], facility_ids=[fid])
                pending.add(fid)
            elif by_id[fid].priority == 1:
                conflict("always_on_in_batch",
                         f"一级用户 {by_id[fid].name} 全程保留，不能进入轮换批次 {b['batch_no']}",
                         batch_no=b["batch_no"], facility_ids=[fid])
                pending.add(fid)

    # 同一用户不能落在重叠时段
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            a, c = parsed[i], parsed[j]
            if a["start"] < c["end"] - TIME_EPS and c["start"] < a["end"] - TIME_EPS:
                for fid in sorted(set(a["ids"]) & set(c["ids"])):
                    name = by_id[fid].name if fid in by_id else str(fid)
                    conflict("facility_overlap",
                             f"用户 {name} 同时落在批次 {a['batch_no']} 与批次 {c['batch_no']} 的重叠时段",
                             batch_nos=[a["batch_no"], c["batch_no"]], facility_ids=[fid])
                    pending.add(fid)

    # 时间轴分段：重叠批次按并集计算同时在供负荷
    segments: list[dict] = []
    if start is not None:
        bounds = sorted({start, end} | {b["start"] for b in parsed} | {b["end"] for b in parsed})
        for x, y in zip(bounds, bounds[1:]):
            active = [b for b in parsed if b["start"] < y - TIME_EPS and b["end"] > x + TIME_EPS]
            if not active:
                continue
            seg_ids: list[int] = []
            for b in active:
                for fid in b["ids"]:
                    if fid in by_id and by_id[fid].priority != 1 and fid not in seg_ids:
                        seg_ids.append(fid)
            load = round(reserved + sum(by_id[f].power_mw for f in seg_ids), 6)
            nos = sorted(b["batch_no"] for b in active)
            if cap is not None and load > cap + EPS:
                conflict("capacity_overload",
                         f"{fmt(x)}–{fmt(y)} 同时在供负荷 {load:g}MW 超过备用容量 {cap:g}MW，缺口 {round(load - cap, 3):g}MW",
                         segment_start=fmt(x), segment_end=fmt(y), batch_nos=nos,
                         facility_ids=seg_ids, shortfall_mw=round(load - cap, 3))
                pending.update(seg_ids)
            segments.append({"start_time": fmt(x), "end_time": fmt(y), "batch_nos": nos,
                             "load_mw": load, "margin_mw": None if cap is None else round(cap - load, 3)})
        if not segments:
            segments.append({"start_time": fmt(start), "end_time": fmt(end), "batch_nos": [],
                             "load_mw": reserved, "margin_mw": None if cap is None else round(cap - reserved, 3)})

    table = []
    for b in parsed:
        members = [by_id[i] for i in b["ids"] if i in by_id]
        load = round(reserved + sum(c.power_mw for c in members if c.priority != 1), 6)
        table.append({"batch_no": b["batch_no"], "start_time": fmt(b["start"]), "end_time": fmt(b["end"]),
                      "facilities": _customer_dicts(members),
                      "load_mw": load, "margin_mw": None if cap is None else round(cap - load, 3)})
    table.sort(key=lambda r: r["batch_no"])

    scheduled = {fid for b in parsed for fid in b["ids"]}
    unscheduled = [{"facility_id": c.id, "name": c.name, "priority": c.priority, "power_mw": c.power_mw,
                    "reason": "未进入任何批次，轮换窗口内全程停供"}
                   for c in sorted((c for c in customers if c.priority in (2, 3) and c.id not in scheduled),
                                   key=lambda c: (c.priority, c.id))]

    return {"feasible": not conflicts, "conflicts": conflicts,
            "pending_facility_ids": sorted(pending),
            "capacity_mw": cap, "start_time": fmt(start) if start else start_s,
            "end_time": fmt(end) if end else end_s,
            "always_on": _customer_dicts(always_on), "reserved_mw": reserved,
            "rotation_table": table, "segments": segments, "unscheduled": unscheduled}


def build_batches(universe: list[dict], capacity_mw: object, start_s: str, end_s: str,
                  slot_minutes: int = DEFAULT_SLOT_MINUTES) -> dict:
    """自动排批：窗口按 slot_minutes 切槽，二级优先于三级，同优先级大功率优先，
    已供次数少的用户在下一槽优先获得电量（轮流）。单户无法容纳时报冲突。
    """
    start, end = parse_ts(start_s), parse_ts(end_s)
    if start >= end:
        raise ValueError("开始时间必须早于结束时间")
    slot = timedelta(minutes=int(slot_minutes))
    if slot <= timedelta(0):
        raise ValueError("批次时长必须为正数")
    cap = float(capacity_mw)  # type: ignore[arg-type]
    if cap <= 0:
        raise ValueError("备用容量必须为正数")

    customers = _customers(universe)
    reserved = round(sum(c.power_mw for c in customers if c.priority == 1), 6)
    # 一级保留负荷已超容量时，二、三级全部无法安排，直接给出保留超限冲突
    if reserved > cap + EPS:
        return evaluate(universe, cap, fmt(start), fmt(end), [])
    rotatable = sorted((c for c in customers if c.priority in (2, 3)),
                       key=lambda c: (c.priority, -c.power_mw, c.id))
    unfit = [c for c in rotatable if reserved + c.power_mw > cap + EPS]
    pool = [c for c in rotatable if c not in unfit]
    served = {c.id: 0 for c in pool}

    slots: list[dict] = []
    cur = start
    while cur < end:
        nxt = min(cur + slot, end)
        used: set[int] = set()
        load = reserved
        ids: list[int] = []
        for c in sorted(pool, key=lambda c: (served[c.id], c.priority, -c.power_mw, c.id)):
            if c.id not in used and load + c.power_mw <= cap + EPS:
                ids.append(c.id)
                used.add(c.id)
                load += c.power_mw
                served[c.id] += 1
        slots.append({"start_time": fmt(cur), "end_time": fmt(nxt), "facility_ids": ids})
        cur = nxt

    result = evaluate(universe, cap, fmt(start), fmt(end),
                      [{"batch_no": i, **s} for i, s in enumerate(slots, 1)])
    for c in unfit:
        result["conflicts"].insert(0, {"code": "facility_unfit",
            "reason": f"用户 {c.name}（{c.power_mw:g}MW）功率超过一级用户保留后的余量 {round(cap - reserved, 3):g}MW，无法排入任何批次",
            "facility_ids": [c.id]})
        result["pending_facility_ids"].append(c.id)
    result["pending_facility_ids"] = sorted(set(result["pending_facility_ids"]))
    result["feasible"] = not result["conflicts"]
    return result
