# 海上搜救协调系统

标准库实现的独立协调原型，使用 SQLite 保存事件、搜救资源、搜索区域、线索、离线批次、时间线，
以及漂移推算结果、区域合并关系和航程放行门控记录。

## 运行

要求 Python 3.11+（在当前 Python 3.9 环境也可运行）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址为 `http://127.0.0.1:8206`，数据库默认为 `maritime_sar.db`。`--db`、`--host`、`--port` 可覆盖默认值。
旧库启动时会自动补加搜索区域的漂移/门控列。

## 漂移推算调度

分层设计，三层互不依赖存储与页面：

- `drift.py`：漂移推算（纯计算）。按事件发生至今的时长（`elapsed_hours`）和事件的
  漂移方向（罗经方位，度）、漂移速度（节）计算漂移距离（1 节 = 1.852 公里/小时），
  在球面上把区域中心平移；概率半径 = 原半径 + 漂移距离 × 发散系数（默认 0.10）。
- `scheduling.py`：调度规则（纯规则）。概率圆重叠判定（圆心距 ≤ 半径和）、重叠圆合并
  （包含关系取大圆，否则取两圆外接圆）、候选资源筛选（能力 + 适航海况）、航程放行门控。
- `app.py`：持久化与编排，`static/index.html` 为只读页面。

`POST /api/areas` 保存一个人手圈定区域时自动执行：

1. 手工区域登记为所属事件的区域，状态转 **待复核（review）**；
2. 按事件发生到保存时刻的经过时长和漂移参数，推算中心与半径，生成一个新的
   **概率区域（probability）**；
3. 与同一事件、同一搜索类型的现有概率区域比较，**重叠即合并**为一个概率区
   （合并结果继续参与合并），源区域状态转 **已合并（merged）**，合并关系写入 `area_merges`；
4. 对具备该类型能力且适航海况的资源逐条做航程门控：只要有一条资源到中心的距离
   不超过其航程，概率区即可放行（`planned`）；全部够不着则留在
   **待放行（pending_release）**，并在 `gate_reason` 和 `area_gate_checks` 中写清
   是哪条资源、差多少公里。

返回：`saved_area`（待复核的旧区）、`probability_area`（最终概率区）、`projection`
（推算参数）、`merges`（本次合并链）、`gate`（逐资源门控结果）。

区域状态：`planned` 可放行、`pending_release` 待放行、`review` 待复核（旧圈定区）、
`merged` 已并入其他概率区、`assigned`/`active` 执行中、`completed`/`abandoned` 已结束。
待复核区、已合并区、待放行区均不能直接分配；待放行区未处理前事件不能关闭。
推算结果、合并关系、门控记录均持久化，页面重开后继续可见（页面含事件、概率区域、
待复核旧区域、合并关系四张视图和资源可达明细）。

## 主要接口

写操作使用 JSON，并需要 `X-User` 与 `X-Role` 请求头。角色包括 `coordinator`、`operator`、`field`、`analyst`、`viewer`。

- `GET /health`、`GET /api/state`
- `POST /api/incidents`：创建遇险事件（含 `drift_direction`、`drift_speed_kn`）并识别重复报警
- `POST /api/assets`：登记资源
- `POST /api/areas`：登记人手圈定区域，保存即漂移推算 + 合并 + 航程门控（见上）
- `POST /api/assignments`：按能力、海况和航程分配概率区域
- `POST /api/clues`、`POST /api/clues/verify`
- `POST /api/assets/withdraw`：撤回资源并释放任务
- `POST /api/incidents/transfer`、`POST /api/incidents/close`
- `POST /api/offline/batch`：幂等合并离线记录
- `GET /api/incidents/{id}/timeline`

## 测试

```bash
python3 -m unittest discover -s tests -v
```

- `tests/test_flow.py`：完整协调流程、重复报警、错误位置、资源并发占用、离线幂等、权限。
- `tests/test_drift_scheduling.py`：漂移推算、重叠合并（含包含关系）、航程门控、
  待复核/待放行约束、重开持久化。

## 局限

身份依赖调用方传入的用户和角色头；坐标使用球面距离近似；漂移按匀速直线流场估算，
未接入实时风/流场模型；文件附件、真实通信链路和地理围栏未包含在内。
