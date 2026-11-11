# 公交数据服务 API

FastAPI 服务，包含两部分功能：

1. **站间客流补充隐藏**：发布站间客流矩阵时保留真实行列合计，先隐藏 `1..k-1` 人的小客流单元，再以**最少格数**补充隐藏其他单元，使每个先隐藏单元在符合公开值及行列合计的所有非负整数矩阵中至少有两个可行值。
2. **维修门户完工记录按月采集**：从维修门户采集已封存月份的完工记录（工单号、车辆号、故障码、完工时间 UTC 秒），供故障率分析入仓。支持断点续采、会话失效自动重登、整页事务提交。

发布矩阵用 `null` 表示隐藏格；响应同时给出每个先隐藏单元的最小、最大可行值，供内部核对。

## 快速启动（Docker Compose）

```bash
docker compose up --build -d
```

Compose 启动两个服务：

- `api`（<http://localhost:8000>）：客流隐藏接口 + 采集/续采/查询接口，文档在 `/docs`；
- `portal`（<http://localhost:8080>）：本地演示维修门户（Cookie 会话 + CSRF 登录表单 + HTML 列表/详情页）。

- Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>
- 可反推示例：<http://localhost:8000/example>

停止：

```bash
docker compose down
```

如需更改宿主机端口：

```bash
HOST_PORT=18080 PORTAL_PORT=18081 docker compose up --build -d
```

镜像基于 `python:3.11-slim`，以非 root 用户运行，并带容器健康检查。采集数据（SQLite）保存在命名卷 `collector-data` 中，容器重启不丢失。

## 维修门户完工记录按月采集

### 接口（均在 `/docs` 可试）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/collection/tasks` | 触发按月采集；失败/中断后再次调用即**续采**；已完成直接返回 `completed` |
| `GET` | `/api/collection/tasks` | 全部任务进度 |
| `GET` | `/api/collection/tasks/{month}` | 单月任务进度（状态、游标页地址、已提交页数/记录数、最近错误） |
| `GET` | `/api/collection/tasks/{month}/records` | 已采完工记录查询（`limit`/`offset` 分页） |

请求体：`{"month": "2025-10"}`（格式 `YYYY-MM`）。

- 首次启动返回 **202** `started`；同任务并发触发只运行一次，其余返回 **200** `running`；
- 任务状态：`running` / `completed` / `failed`（失败或进程中断，可重新触发续采）。

### 采集规则

- HTTP 源地址与演示登录信息由环境变量固定（`PORTAL_BASE_URL`、`PORTAL_USERNAME`、`PORTAL_PASSWORD`）；
- 门户为 Cookie 会话 + 带 CSRF 令牌的登录表单；采集端先取令牌登录，再沿列表页 `rel="next"` 下一页链接取数；
- 从详情页提取唯一工单号、车辆号、故障码、完工时间（UTC 整数秒），并逐字段校验；
- **会话失效跳回登录时**，重新取令牌登录并重取原请求，不当成空页或跳页；每个 HTTP 请求最多重登一次，再失败停在当前页；
- **整页详情全部获取并校验后**，记录与下一页地址在同一事务提交 SQLite；断流、坏详情或进程中断都不推进该页，重启可续采；已提交工单按 `(月份, 工单号)` 主键去重，不重复入库；
- 只运行一个 API 进程（`docker compose` 单副本、单 uvicorn worker），同任务并发去重在进程内完成。

### 演示门户的故障注入

演示门户默认配置可完整演示异常与恢复：

- **翻页前会话过期**：`PORTAL_SESSION_PAGE_BUDGET=1`，每个会话只能服务 1 个列表页，翻页时会话即被强制失效，采集端自动重登续取；
- **某页第二份详情断流**：`PORTAL_FAIL_ONCE_ORDERS=WO-202510-005`（2025-10 第二页的第二份详情），首次请求时连接中断，之后恢复；
- 门户另有 `POST /_demo/reset` 可重置故障注入，供反复演示。

### 演示流程

```bash
docker compose up --build -d

# 1. 触发 2025-10 采集（门户每月 7 份工单、3 页）
curl -s -X POST http://localhost:8000/api/collection/tasks \
  -H 'Content-Type: application/json' -d '{"month":"2025-10"}'
# → 202 started；立即再发一次 → 200 running（同任务只运行一次）

# 2. 首轮会停在第二页：WO-202510-005 首次详情断流
curl -s http://localhost:8000/api/collection/tasks/2025-10
# → status=failed，next_page_url 停在 page=2，pages_committed=1（第一页 3 条已保留）

# 3. 再次触发即续采：从第二页继续，断流详情已恢复
curl -s -X POST http://localhost:8000/api/collection/tasks \
  -H 'Content-Type: application/json' -d '{"month":"2025-10"}'
sleep 5
curl -s http://localhost:8000/api/collection/tasks/2025-10
# → status=completed，total_records=7

# 4. 查询已采记录
curl -s 'http://localhost:8000/api/collection/tasks/2025-10/records?limit=100'

# 5. 进程中断演示：采集中途重启 API，任务被标记为“进程中断”，游标不推进
docker compose restart api
# 重新触发 POST /api/collection/tasks 即可从游标页续采，已提交工单不重复
```

### 采集端配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORTAL_BASE_URL` | `http://localhost:8080` | 门户源地址（Compose 内为 `http://portal:8080`） |
| `PORTAL_USERNAME` | `demo` | 演示登录用户名 |
| `PORTAL_PASSWORD` | `demo123` | 演示登录密码 |
| `COLLECTOR_DB_PATH` | `collector.db` | SQLite 路径（Compose 内为 `/data/collector.db`） |
| `COLLECTOR_HTTP_TIMEOUT` | `10` | 单次 HTTP 请求超时（秒） |

## 请求

`POST /suppress`

```json
{
  "row_stations": ["人民广场", "陆家嘴"],
  "column_stations": ["静安寺", "徐家汇", "世纪大道"],
  "matrix": [[1, 2, 3], [4, 5, 6]],
  "k": 2
}
```

| 字段 | 说明 |
| --- | --- |
| `row_stations` | 行站名，2～3 个，非空 |
| `column_stations` | 列站名，2～4 个，非空 |
| `matrix` | 2～3 行、2～4 列整数矩阵，每格 `0..10000` |
| `k` | 整数阈值，`k >= 2`；值为 `1..k-1` 的单元为先隐藏单元 |

为便于对接，请求也接受这些字段别名：

- `row_stations` / `row_names` / `rowLabels` / `rows`
- `column_stations` / `col_stations` / `column_names` / `cols`
- `matrix` / `flow_matrix` / `flows` / `data`
- `k` / `threshold`

校验失败返回 HTTP `422`。

## 示例调用

```bash
curl -s -X POST http://localhost:8000/suppress \
  -H 'Content-Type: application/json' \
  -d '{
    "row_stations": ["人民广场", "陆家嘴"],
    "column_stations": ["静安寺", "徐家汇", "世纪大道"],
    "matrix": [[1, 2, 3], [4, 5, 6]],
    "k": 2
  }'
```

该示例中 `(0,0)=1` 若只做初次隐藏，会被第一行合计 `6` 与公开值 `2,3` 反推为 `1`，也会被第一列合计 `5` 与公开值 `4` 反推为 `1`。服务以最少 3 格补充隐藏 `(0,1)、(1,0)、(1,1)`，得到：

```json
{
  "status": "ok",
  "message": "已保留行列合计；先隐藏 1 个小客流单元，并以最少 3 格补充隐藏。",
  "k": 2,
  "row_stations": ["人民广场", "陆家嘴"],
  "column_stations": ["静安寺", "徐家汇", "世纪大道"],
  "published_matrix": [[null, null, 3], [null, null, 6]],
  "row_totals": [6, 15],
  "column_totals": [5, 7, 9],
  "primary_suppressed_cells": [
    {
      "row": 0,
      "column": 0,
      "row_station": "人民广场",
      "column_station": "静安寺",
      "suppression_type": "primary",
      "min_feasible": 0,
      "max_feasible": 3,
      "feasible_value_count": 4,
      "protected": true
    }
  ],
  "secondary_suppressed_cells": [
    {"row": 0, "column": 1, "row_station": "人民广场", "column_station": "徐家汇", "suppression_type": "secondary"},
    {"row": 1, "column": 0, "row_station": "陆家嘴", "column_station": "静安寺", "suppression_type": "secondary"},
    {"row": 1, "column": 1, "row_station": "陆家嘴", "column_station": "徐家汇", "suppression_type": "secondary"}
  ],
  "primary_suppressed_count": 1,
  "secondary_suppressed_count": 3,
  "total_suppressed_count": 4
}
```

完整文件见：

- `examples/reverse_inferable_request.json`
- `examples/reverse_inferable_response.json`

## 响应字段

| 字段 | 说明 |
| --- | --- |
| `published_matrix` | 发布矩阵；隐藏格为 `null`，公开值与输入一致 |
| `row_totals` / `column_totals` | 原始真实行列合计，始终发布 |
| `primary_suppressed_cells` | 每个先隐藏单元及其 `min_feasible`、`max_feasible`、可行值个数 |
| `secondary_suppressed_cells` | 为保护先隐藏单元而补充隐藏的单元 |
| `*_count` | 初次/补充/合计隐藏格数 |

响应不返回被隐藏单元的原始值。

## 无解

若补充隐藏所有其他单元后仍有先隐藏单元只能取唯一值，例如某行合计为 `0` 导致同列小客流被结构性地固定，则返回 HTTP `422`：

```json
{
  "detail": {
    "status": "no_solution",
    "message": "无法保留这些行列合计发布：即使补充隐藏所有非初次隐藏单元，仍有先隐藏单元只能取唯一值。",
    "reason": "无法在保留公开行列合计的前提下保护所有小客流单元",
    "unprotectable_cells": [
      {"row": 0, "column": 1, "row_station": "甲站", "column_station": "丁站"}
    ]
  }
}
```

示例见 `examples/no_solution_request.json` 与 `examples/no_solution_response.json`。

## 求解规则

1. 先隐藏集合 `P = {(i,j) : 1 <= a[i][j] <= k-1}`。
2. 对非先隐藏单元的每个子集 `S`，按 `|S|` 从小到大搜索；一旦某 `S` 能保护所有 `P` 中单元，即得到最少补充隐藏数。
3. 固定某组隐藏格后，把行、列剩余合计作为供给/需求，建立二部网络流。对每个先隐藏单元，用整数最大流判定其是否可等于某值；可行值在运输多面体的一条边上构成整数区间，因此二分求最小/最大可行值。
4. 保护条件为 `max_feasible > min_feasible`，即至少两个可行值。
5. 若无任何 `S` 成功，返回无法保留合计发布的原因。

矩阵至多 `3×4 = 12` 格，搜索和网络流规模都很小。

## 本地开发与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

# 终端 1：演示门户（默认 8080）
uvicorn portal.app:app --reload --port 8080
# 终端 2：主 API（默认 8000）
uvicorn app.main:app --reload

python -m unittest discover -s tests -v
```

也可以直接启动容器后访问 `/docs` 的 “Try it out”。

## 目录

```text
app/
  main.py            FastAPI、请求/响应模型、文档示例、采集路由挂载
  solver.py          初次隐藏、最少补充隐藏、整数可行性区间
  collector/
    config.py        采集配置（门户地址、演示登录、SQLite 路径）
    portal_client.py CSRF 登录、Cookie 会话、失效重登一次的 HTTP 客户端
    parsing.py       列表页/详情页/登录页 HTML 解析与字段校验
    storage.py       SQLite：完工记录 + 当前页游标，整页事务提交
    service.py       任务管理：并发去重、采集主循环、断点续采
    routes.py        /api/collection 采集、续采、进度与记录查询
portal/
  app.py             本地演示维修门户（会话过期/详情断流故障注入）
  data.py            确定性演示数据集（已封存月份完工工单）
  Dockerfile
tests/
  test_solver.py
  test_api.py
  test_parsing.py
  test_storage.py
  test_portal.py
  test_collector.py
  test_collection_api.py
examples/
  reverse_inferable_*.json
  no_solution_*.json
Dockerfile
docker-compose.yml
requirements.txt
```
