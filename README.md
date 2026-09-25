# 海上搜救协调系统

标准库实现的独立协调原型，使用 SQLite 保存事件、搜救资源、搜索区域、线索、离线批次和时间线。

## 模块划分

- `drift.py`：漂移推算。按事件发生到现在经过的时长，结合漂移方向和漂移速度推算概率区域的中心与半径（纯函数，不含数据库与 HTTP）。
- `dispatch.py`：调度规则。概率区域重叠合并（最小包围圆）、资源航程放行判断（哪条资源够不着）。
- `app.py`：服务与 HTTP 层。持久化、权限、审计，把推算和调度规则接入业务流程。
- `static/index.html`：页面。展示推算结果、合并关系和待放行区域，刷新后从 SQLite 重新读取。

## 运行

要求 Python 3.11+（在当前 Python 3.9 环境也可运行）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址为 `http://127.0.0.1:8206`，数据库默认为 `maritime_sar.db`。`--db`、`--host`、`--port` 可覆盖默认值。

## 漂移推算调度

每个搜索区域都登记所属事件。保存人工区域（或对事件调用重算接口）时：

1. 按 `事件发生时长 × 漂移速度` 沿漂移方向移动事发位置得到新中心；半径 = 初始不确定半径 + 漂移距离 × 10% + 0.5km/小时扩散，生成新的概率区域（`origin=drift`，编号 `PRJ-<事件>-<序号>`）。
2. 上一版仍存活的概率区域：与新区域重叠的合并进来（状态 `merged`，`merged_into` 指向新区域）；不重叠的转为待复核（`pending_review`）。
3. 推算中心只要超出某条可用且具备能力的资源的航程，新区域先留在待放行（`pending_release`），`hold_reason` 写清是哪条资源够不着（名称、距离、航程）；协调员放行（`POST /api/areas/release`）后转为 `planned` 才能分配。

推算结果、合并关系和待放行原因都持久化在 `search_areas` 表中，页面重开后继续可见。区域状态机：`planned` → `assigned`/`active`，以及 `pending_review`（待复核）、`pending_release`（待放行）、`merged`（已合并）、`completed`/`abandoned`。待放行区域会阻止事件关闭，待复核与已合并不阻止。

## 主要接口

写操作使用 JSON，并需要 `X-User` 与 `X-Role` 请求头。角色包括 `coordinator`、`operator`、`field`、`analyst`、`viewer`。

- `GET /health`、`GET /api/state`
- `POST /api/incidents`：创建遇险事件并识别重复报警
- `POST /api/assets`：登记资源
- `POST /api/areas`：创建搜索区域（保存时自动推算概率区域）
- `POST /api/incidents/reproject`：按当前时刻重新推算事件的概率区域
- `POST /api/areas/release`：放行待放行区域
- `POST /api/assignments`：按能力、海况和航程分配资源
- `POST /api/clues`、`POST /api/clues/verify`
- `POST /api/assets/withdraw`：撤回资源并释放任务
- `POST /api/incidents/transfer`、`POST /api/incidents/close`
- `POST /api/offline/batch`：幂等合并离线记录
- `GET /api/incidents/{id}/timeline`

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整协调流程、重复报警、错误位置、资源并发占用、离线幂等、权限拒绝，以及漂移推算（保存时生成概率区域）、重叠合并、不重叠转待复核、超航程待放行与放行、推算结果重开持久化。

## 局限

身份依赖调用方传入的用户和角色头；坐标使用球面距离近似；漂移模型为匀速直线推算加固定扩散系数，未接入实时风流场；文件附件、气象服务、真实通信链路和地理围栏未包含在内。
