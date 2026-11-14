# PM2.5 小时观测发布台

站点小时 PM2.5 观测的单日 CSV 上传、预览确认与按日 JSON 发布服务。
后端 FastAPI + SQLite，前端为同源静态页面，无外部前端依赖。

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>。数据保存在命名卷 `pm25-data`（`/data/pm25.db`），
`docker compose down` 不会删除数据；需清空时使用 `docker compose down -v`。

不使用 Docker 时：

```bash
python3 -m pip install -r requirements.txt
PM25_DB_PATH=./data/pm25.db uvicorn app.main:app --reload
```

测试：

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest
```

测试覆盖：格式错误整批拒绝、批内最高修订号、同修订号冲突、已发布修订号冲突、
预览跨发布失效、并发确认只有一个成功、重复确认幂等、发布中途失败回滚；
站点归并的逐小时取高修订号、同修订号冲突指出时刻并拒绝、原始记录不变、
原编号来源标注、任一编号补报按合并身份预览与发布、归并与补报使旧预览失效、
并发归并/确认的一致结果。

## CSV 格式

单次上传只允许一个观测日期。表头必须包含四列（支持中英文别名，列顺序不限）：

| 列 | 别名示例 | 要求 |
| --- | --- | --- |
| 站点 | `station`、`站点` | 非空文本，≤120 字符 |
| 整点时间 | `timestamp`、`整点时间` | ISO 整点（`2026-11-13T05:00:00`）；也可配 `date` 列用 `05:00`。接受省略秒、以 `Z`/`+00:00` 结尾、小数秒为 0 的形式 |
| 修订号 | `revision`、`修订号` | 非负整数 |
| 数值 | `value`、`pm2.5`、`数值` | 非负有限数字，最多 9 位小数，规范化为普通十进制 |

可选 `date` 列（`2026-11-13`）。支持 UTF-8（含 BOM）与 GB18030，分隔符可为逗号、分号、
制表符或竖线；按内容自动识别。文件上限 5 MiB / 20,000 行。

示例：

```csv
station,timestamp,revision,value
SY-001,2026-11-13T00:00:00,1,12.4
SY-001,2026-11-13T01:00:00,1,10.8
SY-001,2026-11-13T01:00:00,2,11.2
SY-002,2026-11-13T00:00:00,3,18.0
```

## 规则

- **同站同小时以最高修订号为准。** 批内低修订号标记为“忽略（superseded_in_batch）”，
  同修订号的重复行标记为“忽略（duplicate_in_batch）”。
- **同修订号数值不一致则整批拒绝。** 包括：批内同一键同一修订号对应不同数值；以及上传值
  与已发布记录修订号相同但数值不同。此时返回 `409 batch_conflict`，不创建可确认的预览，
  不写入任何数据。展示出的新增/替换只是“若无冲突将会执行”的分类结果。
- **相对已发布数据：** 更高修订号 → “替换”；相同修订号且数值相同 → “忽略（already_published）”；
  更低修订号 → “忽略（older_than_published）”，不会覆盖既有记录。
- 新增与替换记录合计为“待发布”；无待发布记录时确认按钮不可用。

## 站点编号归并

监测站更换编号后，可在页面“站点归并”中选择两个**尚未归并**的编号（旧编号 → 现用编号），
生成归并预览后确认。归并只改变读数时使用的站点身份，**原始已发布记录不改**。

- 归并预览按 `(日期, 小时)` 合并两个编号的读数：**同一小时取较高修订号**。
- 若同一小时的**最高修订号相同但数值不同**，归并拒绝（`409 merge_conflict`），
  并在 `detail.conflicts` 中逐条指出日期、时刻、修订号与两个编号各自的数值；此时不建立归并。
- 每条读数保留来源：`source` 为 `old` / `new` / `both`，`source_station` 为实际写入该记录的
  原编号。归并后任一编号的补报会物理写入现用编号，但 `source_station` 仍标注提交时使用的编号。
- 确认归并会为受影响日期递增日版本，使归并前生成的发布预览立即失效；归并预览自身记录
  生成时相关日期的日版本与行数快照，此后若有补报发布，确认返回 `409 merge_stale`。
- 归并预览确认幂等：重复确认返回 `200`、`already_merged: true` 并回放首次结果。
- 并发确认按实际提交顺序串行化：重叠编号的两个归并、同一归并的多个确认，
  都只有按实际顺序的第一个成功，其余得到 `409` 或幂等结果。

`GET /api/stations` 返回可归并编号与已归并分组；`GET /api/stations/{station}` 返回该身份
（含旧、新编号）的全部原始读数。`GET /api/merged/{station}/days/{YYYY-MM-DD}` 按现用编号
返回某日合并读数及每条来源，映射与读数在同一事务快照中读取。

## 预览与失效

`POST /api/previews` 解析 CSV、按日版本号生成快照并写入预览表（不修改已发布数据）。
每日维护一个 `day_version`，每次成功发布递增。确认时以 `BEGIN IMMEDIATE` 锁库并核对
`day_version`：如果预览生成后该日已被他人发布，旧预览立即返回 `409 preview_stale`，
且不会再被写入。被判定失效的预览永久保持 `stale`，需重新上传生成新预览。

## 发布语义

- 确认在单个 SQLite 事务内完成：写入全部待发布记录、递增日版本、标记预览已发布。
  任一步失败整体回滚，不影响已发布记录，预览保持可重试的 `preview` 状态。
- 同一预览重复确认不会再次发布：已发布时返回 `200`、`already_published: true`，并回放
  首次发布结果；数据与日版本均不变。
- 格式错误不创建预览，也不触碰已发布数据。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 上传与浏览页面 |
| `GET` | `/api/health` | 健康检查 |
| `POST` | `/api/previews` | 上传 CSV（`multipart/form-data`，字段 `file`），`201` 返回预览 |
| `GET` | `/api/previews/{id}` | 读取预览及当前状态 |
| `POST` | `/api/previews/{id}/confirm` | 确认发布（幂等） |
| `GET` | `/api/stations` | 可归并站点与已归并分组 |
| `GET` | `/api/stations/{station}` | 该站点身份（含旧编号）的全部原始读数 |
| `POST` | `/api/merges` | 生成归并预览，JSON `{"old_station": "...", "new_station": "..."}` |
| `GET` | `/api/merges/{id}` | 读取归并预览及当前状态 |
| `POST` | `/api/merges/{id}/confirm` | 确认归并（幂等） |
| `GET` | `/api/merged/{station}/days/{YYYY-MM-DD}` | 按现用编号读取该日合并读数 |
| `GET` | `/api/merged/{station}/days/{YYYY-MM-DD}.json` | 同上，附件下载 |
| `GET` | `/api/days` | 已发布日期列表 |
| `GET` | `/api/days/{YYYY-MM-DD}` | 该日已发布 JSON |
| `GET` | `/api/days/{YYYY-MM-DD}.json` | 同上，附件下载 |

按日查询响应：

```json
{
  "date": "2026-11-13",
  "day_version": 1,
  "count": 2,
  "records": [
    {"station": "SY-001", "hour": "01:00", "timestamp": "2026-11-13T01:00:00",
     "revision": 2, "value": "11.2", "batch_id": "...", "updated_at": "..."}
  ]
}
```

数值以规范化字符串返回，避免二进制浮点误差；下游按需自行转 `float`/`Decimal`。

合并读数响应（`/api/merged/...`）：

```json
{
  "station": "NEW-001",
  "current_station": "NEW-001",
  "former_stations": ["OLD-001"],
  "date": "2026-11-13",
  "day_version": 2,
  "count": 3,
  "records": [
    {"station": "NEW-001", "hour": "01:00", "timestamp": "2026-11-13T01:00:00",
     "revision": 2, "value": "11.2", "source_station": "OLD-001", "sources": ["OLD-001"]}
  ],
  "conflicts": []
}
```

归并预览响应包含 `cells`（每个小时一条，含 `revision`、`value`、`source`、`source_station`、
`sources`、`conflict`）与 `conflicts`（冲突时刻明细），以及 `counts`：

```json
{"cells": 3, "from_old": 1, "from_new": 2, "from_both": 0, "conflicts": 0}
```
