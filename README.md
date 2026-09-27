# 电网事故应急与恢复调度系统

标准库 Python 3.11+ + SQLite。系统管理停运事故、重要用户、备用容量、恢复步骤及安全依赖；接受现场离线报告并区分已合并、版本冲突和受保护记录，异常遥测单独隔离。事故后备用容量不足时，支持按事故、起止时间和容量编制轮换保供安排：一级用户全程保留，二、三级用户按功率轮流分批。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8215`。身份使用 `X-Actor` 和 `X-Role`，角色为 `dispatcher`、`operator`、`field`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/assets`、`POST /api/facilities`：登记线路资产和医院等重要用户。
- `POST /api/outages`：创建或幂等接收同一事故。
- `POST /api/telemetry`：记录并隔离错误遥测。
- `POST /api/plans`、`/submit`、`/approve`、`/activate`：创建、提交、审批并启用安全恢复计划。
- `POST /api/plans/{id}/change`：在不修改已确认步骤的前提下创建新计划版本。
- `POST /api/field-reports`：合并现场离线报告，重复客户端编号不会重复写入。
- `POST /api/plans/{id}/confirm`：调度员确认步骤，依赖未满足时拒绝。
- `POST /api/status`：发布当前恢复状态。
- `POST /api/facilities/{id}/power`：修改用户功率；变化会使旧轮换安排失效。

### 轮换保供

- `POST /api/rotations/preview`：校核候选安排（自动排批或手工批次），只返回轮换表、分段余量、冲突与待调整用户，**不写入**。
- `POST /api/rotations`：校核通过才写入，冲突时 409 返回候选与原因；同一事故仅保留一份 active，新安排取代旧安排。
- `POST /api/supply`：现场接通/停供后更新在供清单，返回实际在供负荷与余量，并标记与当前时段计划不符的条目。
- `GET /api/rotations`、`GET /api/rotations/{id}`：轮换清单与详情（轮换表、分段余量、在供清单、失效原因）。

规则（一级全程保留、二三级轮排、重叠时段禁止同人、容量并集校核、失效判定）全部在 `rotation.py` 纯函数模块；`app.py` 只负责存储与服务，`static/index.html` 只负责展示与操作。用户功率或优先级变化、恢复计划版本变化都会让旧安排失效（只读，需重新编制）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_rotation.py` 覆盖：一级保留与禁入轮换批、重叠批次并集超载、同一用户重叠时段、自动轮流排批、冲突候选不落库、现场在供更新、功率/计划版本变化失效和角色权限。

当前为原型：容量和依赖是静态安全模型，不包含潮流计算、SCADA/EMS 协议、实时遥测质量码或生产级多实例锁；离线合并通过客户端编号和计划版本完成。
