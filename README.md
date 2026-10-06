# 应急无线电演练通联日志互证服务

为应急无线电演练提供台站通联日志（QSO Log）的**互证配对**服务：
各台站上报自己的通联日志，系统把“**相互指认、波段一致、模式一致、
时差不超过演练允许值**”的两条日志配成一对，并按全场最优规则求解，
返回配对清单与未配对记录。

技术栈：**Python 3.11 + FastAPI + SQLite（WAL）**，容器一键启动，
无外部依赖。

---

## 1. 配对规则与优化目标

两条日志可配对，当且仅当：

1. **相互指认**：一条的本台呼号 == 另一条的对方呼号，反之亦然；
2. **波段相同**；
3. **模式相同**；
4. 两条**带时区时间**的绝对时差 `|t1 − t2|` 不超过演练配置的允许时差；
5. 每条日志最多参与一次配对。

全场求解按以下优先级取**唯一确定**的最优解：

1. **最大化配对数量**；
2. 配对数相同则**最小化总时差**（所有配对时差之和）；
3. 仍并列时取**字典序最小**的配对列表：先把每对内部的两个记录号
   按升序排列，再把所有配对按升序排列，按列表字典序比较取最小。

算法为二分图上的**最小费用最大流**（Dijkstra + potential），
见 [`app/matching.py`](app/matching.py)：相互指认关系天然构成二分图，
费用中对时差主项放大 `2^E` 倍并叠加“配对键等级奖励”，使一次求流
同时满足上述三级目标。配套 150 组随机用例与穷举枚举结果逐一对照
（`tests/test_matching.py`）。

### 人工锁定（复核员互证）

演练复核员可在**未封存**演练中，按两个记录号**锁定**一对互证候选；
锁定对会**强制入选**互证结果：

- 锁定仅接受同时满足下列条件的两条日志：**现存**、**相互指认**、
  **波段相同**、**模式相同**、**时差不超过演练允许值**，且两条记录
  **均未被其他锁定占用**、**均未被隔离**；
- 重算时**先保留全部锁定对**（其记录移出自由匹配池），再对剩余日志
  沿用原“配对数最多 → 总时差最小 → 记录号字典序最小”规则；
  **即使锁定使全场配对数下降，也绝不替换锁定对**；
- **重复锁定同一对视为重传**（幂等，记录号顺序任意），不推进版本；
- **解除不存在的锁定明确报错**（`404 lock_not_found`）；
- 单条或批量**更正/写入使任一锁定失效**（记录被改到不互指、波段/模式
  变化、时差超限等）时，**整次写入拒绝、状态不变**（`409
  lock_invalidated`，事务回滚）；
- **锁定状态变化与结果、版本原子提交**；无状态变化不推进版本；
  **封存后拒绝一切锁定操作**（仍可查询锁定清单与最终结果）；
- 无锁定时结果与历史行为**完全一致**，现有写入与结果查询的
  请求/响应保持兼容。

### 版本快照（复核员按版本回看 / 比较）

每个真正推进的版本都在**同一事务**留存一份不可变快照，供复核员回看与
两版比较：

- **创建演练即留存 0 版快照**（空场：无配对、无未配对、无锁定、无隔离）；
- 每次**日志写入/更正真正推进版本**、**锁定/解除锁定推进版本**、
  **隔离/恢复推进版本**时，同事务留存该版的**配对（含时差）、未配对
  记录、总时差、锁定清单与隔离清单**；
- **同内容重传、失败回滚（含整批拒绝、锁定校验失败、解除不存在的锁定）、
  封存均不产生新快照**；封存本身不改版本，快照中的**封存状态仍从现态读取**；
- **升级既有服务时**：旧库在启动初始化阶段为每个演练从**升级时的当前
  版本**开始留存一份快照，**更早的版本不用现态补造**（取回/比较均明确
  报错）；升级后的版本推进照常逐版留存；
- 取回快照与比较版本都是**只读**操作，不改变当前状态、版本与锁定。

### 版本语义

- 每条日志以 `(演练号, 记录号)` 唯一标识；
- **同内容重传不推进版本**（呼号大小写、首尾空白等规范化后相同即视为重传）；
- **异内容视为更正**：更新日志后**重算全场**，`version + 1`，
  配对结果与新版本在同一事务原子落库；
- **锁定/解除成功各 `version + 1`**，锁定状态、配对结果与版本同事务
  原子提交；重复锁定同一对为重传，不推进版本；
- 批量写入整体在一个事务内：任一条非法（含批次内记录号重复）则
  **整批回滚**；若写入/更正会使**任一既有锁定失效**，同样整批拒绝、
  状态不变（`409 lock_invalidated`）；
- **批量预演与确认**：复核员提交前可先预演同一批日志，预演**只读**，
  返回绑定“演练 + 基准版本 + 规范化批次”的校验标识；确认时三者任一
  对不上（含演练已封存）即拒绝且**状态不变**，通过后按批量写入语义
  原子落库（详见 5.6 节）；
- 并发写入通过 SQLite `BEGIN IMMEDIATE` 串行化，版本号与配对结果
  **原子更新**，不会出现版本与结果错位；
- **封存**演练后拒绝一切写入（返回 `409`），但仍可查询最终结果；
  封存本身不改变配对结果与版本，也**不产生新版本快照**；
- **跨演练迁移**（日志归错演练）在**单个跨演练事务**内把一批记录从
  源演练移至目标演练：源、目标**各只推进一次版本**并分别重算、留存
  新版本快照；日志原字段保留，按目标演练允许时差重新互证；校验不过
  （两端状态问题、源记录缺失、目标同号、记录被源锁定占用）整次
  拒绝、两端状态不变；
- 每个真正推进的版本（含创建时的 0 版）在同事务留存**版本快照**
  （配对/未配对/总时差/锁定清单/隔离清单）；重传与失败回滚不留存，
  升级既有库仅从升级时当前版本开始留存、旧版本不补造；
- **争议日志隔离/恢复**：批次中已处于目标状态的记录自动跳过，仅实际
  状态变化时 `version + 1`（重复操作不推进、不留存快照）；被隔离记录
  不参与配对、不进入未配对清单/缺口诊断、不计入台站汇总；结果与隔离
  清单同事务写入新版本快照（详见 5.10 节）。
- **复核员批量裁决**：锁定/解除与隔离/恢复意图一次提交，按**最终状态**
  整批判定（意图顺序无关、同向去重、相反意图拒绝）；仅实际状态变化时
  在单个事务内重算、`version + 1`（整批只推进一次）并留存对应快照，
  纯重传不推进；基准版本过期或演练已封存整批拒绝（详见 5.11 节）。

### 未互证日志缺口诊断（复核员查看未配对缺口）

复核员可按演练查看当前每条**未配对**记录“为什么没配上”。对每条未配对
记录，按**当前日志**依次归因（命中前者即不再下探）：

1. **候选已占用**（`candidate_occupied`）：存在完全符合互指、波段、模式、
   时差条件的对方记录却未入选——最大配对数解中该候选必然已被别的配对
   （含锁定对）占用，否则增广这条边即可多配一对；
2. **时差超限**（`time_diff_exceeded`）：否则存在互指且波段、模式相同的
   对方记录（时差必然均超过演练允许值）；
3. **波段或模式不符**（`band_or_mode_mismatch`）：否则存在互指记录
   （但波段或模式不一致）；
4. **缺少对方日志**（`missing_counterpart_log`）：没有任何互指记录。

前三类返回**该类候选中时差最小、再按对方记录号最小**的那一条对方记录及
实际时差；末类对方记录为空。结果**按记录号升序**，并带**当前版本**。
诊断为**只读**：不改变日志、结果、版本与快照；写入、更正、锁定等变化
后再查即反映现态；**封存后仍可查**；未知演练明确报错
（`404 drill_not_found`，详见 5.7 节）。

### 按台站汇总（演练结果台站统计）

按演练把**当前日志与互证结果**汇总到台站维度：以每条日志的**本台呼号**
归组，逐台站返回**日志总数、已配对数、未配对数与配对覆盖率**
（已配对数 / 日志总数，保留 6 位小数）：

- **一对互证记录分别计入两端台站**的已配对数；
- **未配对记录只计入其本台**；仅在对方呼号中出现而未上报日志的台站
  **不产生汇总项**；
- 台站**按呼号升序**返回，并带**当前版本与封存状态**；
- 汇总为**只读**：不改变日志、结果、版本与快照；**封存后仍可查**；
  未知演练 `404 drill_not_found`；空演练返回空汇总（详见 5.8 节）。

### 日志归错演练：跨演练迁移（复核员一次性改派记录）

复核员发现日志归错演练时，可把**一批指定记录**一次性从**源演练**移到
**目标演练**。迁移是**跨演练的原子操作**：

- 请求须给出**两个不同演练号**与一份**非空、无重复**的记录号列表；
- 仅当下列条件**全部**满足时才受理，否则**整次拒绝、两端状态不变**：
  1. 源、目标演练**均存在且均未封存**，且演练号不同；
  2. 列表中每条记录在**源演练全部存在**；
  3. 目标演练中**没有任何同号记录**（不覆盖目标日志）；
  4. 被移记录**均不参与源演练的人工锁定**（即不是任一锁定对的成员；
     **未迁移记录上的锁定照常生效**，不受影响）；
  5. 被移记录**均未处于隔离状态**（隔离记录不得跨演练迁移，须先恢复）；
- 成功时被移日志的**原字段原样保留**（呼号/波段/模式、换算为 UTC 的
  时间、`updated_at` 均不变，不做二次规范化），从源演练删除并写入
  目标演练；两端都按**各自演练的允许时差**重新参与互证：
  - 源、目标**各只推进一次版本**，分别**全场重算一次**，并在
    **同一事务**留存各自的**新版本快照**；
  - 双方的**日志、版本、结果与快照原子提交**：任一环节失败整批回滚，
    不会出现“只迁走一半”或版本与结果错位；
  - 目标演练**既有锁定不受影响**（迁入记录只可能与目标的未锁定记录
    自由配对）；源演练未涉及的锁定强制保留；
  - **历史快照不可改写**：迁移前各版本快照保持迁移时点的内容不变；
- 响应回显迁移记录号（请求顺序、去重后）与**两端的新版本、重算后的
  完整互证结果**（详见 5.9 节）。现有日志写入、结果与快照查询
  接口保持兼容。

### 争议日志隔离（复核员批量隔离 / 恢复）

复核员可在**未封存**演练中按记录号**批量隔离**争议日志，争议解决后
**批量恢复**，并可随时查询**按记录号升序**的隔离清单：

- 隔离**只改变互证资格**：不删除日志、不改写任何原字段；被隔离记录
  **不参与配对、不进入未配对清单与缺口诊断**（缺口诊断的候选也仅限
  未隔离日志），也**不计入台站汇总**的日志总数、已配对数与未配对数；
- 隔离期间仍可**按原接口更正日志**（单条/批量/预演确认），隔离状态
  保留；**恢复后按当前日志重新求解**全场互证；
- 隔离与人工锁定**互斥**：隔离记录不得人工锁定或跨演练迁移，
  **已有锁定成员不得隔离**（须先解除锁定）；
- 批次中**记录不存在、编号重复、涉及锁定或演练已封存**时**整批拒绝、
  状态不变**；
- **仅实际状态变化推进一次版本**：批次中已处于目标状态的记录自动跳过；
  有实际变化时 `version + 1`，重算结果与**隔离清单一并写入新版本快照**
  （同一事务）；整批均为重复操作时**不推进版本、不留存快照**；
  **历史快照保持原貌**；
- 批量写入**预演与确认同样遵守隔离状态**（被隔离记录在拟写入结果中
  仍不参与配对）；现有写入、结果与快照接口保持兼容（详见 5.10 节）。

### 复核员批量裁决（锁定/解除与隔离/恢复一次提交）

复核员可把**相互牵连的裁决**一次提交：输入**演练号、基准版本**以及
**锁定/解除记录对**与**隔离/恢复记录**两份意图列表，整批在**单个
事务**内按**最终状态**判定（详见 5.11 节）：

- **意图顺序不影响结果**；**重复同向意图只算一次**；同一对象（记录对
  /记录）出现**相反意图**（既锁定又解除、既隔离又恢复）**整批拒绝**；
- 任一意图**引用不存在的记录**、**最终锁定对不满足既有互证候选条件**
  （现存/互指/波段模式/时差）、**同一记录被多对锁定**、最终锁定对成员
  **仍处于隔离状态**，均**整批拒绝且状态不变**；**演练已封存**或
  **基准版本过期**（与当前版本不一致）亦然；
- 按最终状态合成，因此**同批解除旧锁并隔离、恢复并锁定均可**受理；
  解除本就不存在的锁定、恢复本未隔离的记录是幂等空操作；
- **仅实际状态变化**才在单个事务内**重算互证、推进一次版本**（整批
  只 +1）并**留存对应快照**（含锁定与隔离清单）；**纯重传不推进**；
- 响应返回**当前版本、锁定与隔离清单及完整互证结果**，并回显本批
  实际生效的锁定/解除/隔离/恢复；既有单项锁定/隔离接口及响应契约
  保持兼容。

---

## 2. 一键启动（Docker Compose，推荐）

```bash
docker compose up -d --build
```

启动后：

- 服务地址：`http://127.0.0.1:8080`
- 健康检查：`GET http://127.0.0.1:8080/health`
- 交互式 API 文档（Swagger UI）：`http://127.0.0.1:8080/docs`
- OpenAPI 描述：`http://127.0.0.1:8080/openapi.json`

容器启动时自动完成**依赖安装与数据初始化**（建库、建表、开启 WAL，
幂等）。SQLite 数据保存在命名卷 `qso-data`（容器内 `/data/qso.db`），
容器重建数据不丢。

停止 / 清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷
```

### 仅用 Docker

```bash
docker build -t qso-log-attestation:1.2.0 .
docker run -d --name qso -p 8080:8080 -v qso-data:/data qso-log-attestation:1.2.0
```

---

## 3. 本地直接运行（无需 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 可选：初始化数据库（不执行也会在服务启动时自动初始化）
python -m scripts.init_db

# 启动
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

跑测试：

```bash
pip install pytest httpx
pytest -q
```

一键演示（需要先启动服务，自动创建演练、写入、更正、封存并打印结果）：

```bash
python scripts/demo.py http://127.0.0.1:8080
```

---

## 4. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QSO_HOST` | `0.0.0.0` | 服务监听地址 |
| `QSO_PORT` | `8080` | 服务监听端口（Compose 宿主机映射端口也可用它覆盖） |
| `QSO_DB_PATH` | `/data/qso.db` | SQLite 数据库文件路径（会在同目录产生 `-wal`/`-shm` 文件） |

Compose 改宿主机端口示例：

```bash
QSO_PORT=9000 docker compose up -d --build   # 宿主机 http://127.0.0.1:9000
```

---

## 5. HTTP API

所有请求/响应均为 JSON，时间字段使用 **ISO 8601 且必须携带时区**，
如 `2026-10-04T08:00:00+08:00` 或 `2026-10-04T00:00:00Z`
（内部统一换算为 UTC，微秒精度比较时差）。

可直接导入 [`examples/requests.http`](examples/requests.http) 执行；
也可用 [`scripts/demo.py`](scripts/demo.py) 看完整调用序列。

### 5.1 演练管理

#### `POST /drills` —— 创建演练

```bash
curl -X POST http://127.0.0.1:8080/drills \
  -H 'Content-Type: application/json' \
  -d '{"drill_id": "DRILL-2026", "allowed_time_diff_seconds": 10}'
```

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `drill_id` | string | 演练号，唯一；字母/数字/`_`/`-`/`.` |
| `allowed_time_diff_seconds` | number | 允许时差（秒），**必须非负** |

响应 `201`：

```json
{
  "drill_id": "DRILL-2026",
  "allowed_time_diff_seconds": 10.0,
  "version": 0,
  "sealed": false,
  "created_at": "2026-10-04T12:00:00+00:00",
  "sealed_at": null
}
```

#### `GET /drills` —— 列出演练
#### `GET /drills/{drill_id}` —— 查询演练配置/状态（未知演练 `404`）
#### `POST /drills/{drill_id}/seal` —— 封存演练（幂等封存，重复封存 `409`）

### 5.2 日志写入

#### `PUT /drills/{drill_id}/logs/{record_no}` —— 写入/更正单条日志

路径上的 `record_no` 为准。响应直接返回写入后的最新互证结果。

```bash
curl -X PUT http://127.0.0.1:8080/drills/DRILL-2026/logs/R1 \
  -H 'Content-Type: application/json' \
  -d '{
    "record_no": "R1",
    "station": "BH1AAA",
    "counterpart": "BH2BBB",
    "time": "2026-10-04T08:00:00+08:00",
    "band": "40m",
    "mode": "SSB"
  }'
```

字段：`record_no`（演练内唯一记录号）、`station`（本台呼号）、
`counterpart`（对方呼号）、`time`（**带时区**时间）、`band`、`mode`。
呼号/波段/模式自动 `strip + 大写` 规范化。

#### `POST /drills/{drill_id}/logs` —— 批量写入/更正

```bash
curl -X POST http://127.0.0.1:8080/drills/DRILL-2026/logs \
  -H 'Content-Type: application/json' \
  -d '{"logs": [ { ... }, { ... } ]}'
```

整体一个事务；批次内同记录号重复或任一条非法 => 整批拒绝回滚。

#### `GET /drills/{drill_id}/logs` —— 查看演练全部日志

### 5.3 结果查询

#### `GET /drills/{drill_id}/result` —— 查询互证结果（封存后即最终结果）

```json
{
  "drill_id": "DRILL-2026",
  "version": 3,
  "sealed": false,
  "pairs": [
    {"record_a": "R1", "record_b": "R2", "time_diff_seconds": 5.0},
    {"record_a": "R3", "record_b": "R6", "time_diff_seconds": 8.0}
  ],
  "unpaired": ["R7"],
  "total_time_diff_seconds": 13.0,
  "recomputed_at": "2026-10-04T12:00:05.123456+00:00"
}
```

- `pairs`：每对记录号已内部升序，列表整体升序；
- `unpaired`：未参与任何配对的记录号（按写入顺序）；
- `version`：当前全场版本，同内容重传不变，异内容更正 +1。

### 5.4 人工锁定（复核员互证）

记录号通过请求体传递（避免 `/`、`.` 等记录号字符进入路径）；
`record_a` / `record_b` 顺序任意，服务端按字典序内部升序存储。

#### `PUT /drills/{drill_id}/locks` —— 锁定一对互证候选

```bash
curl -X PUT http://127.0.0.1:8080/drills/DRILL-2026/locks \
  -H 'Content-Type: application/json' \
  -d '{"record_a": "R1", "record_b": "R2"}'
```

响应 `200`（同时回传当前锁定对、锁定清单与最新互证结果）：

```json
{
  "lock": {
    "record_a": "R1",
    "record_b": "R2",
    "time_diff_seconds": 5.0,
    "locked_at": "2026-10-04T12:00:05.000000+00:00"
  },
  "locks": [
    {"record_a": "R1", "record_b": "R2",
     "time_diff_seconds": 5.0,
     "locked_at": "2026-10-04T12:00:05.000000+00:00"}
  ],
  "result": {
    "drill_id": "DRILL-2026",
    "version": 4,
    "sealed": false,
    "pairs": [{"record_a": "R1", "record_b": "R2", "time_diff_seconds": 5.0}],
    "unpaired": ["R3"],
    "total_time_diff_seconds": 5.0,
    "recomputed_at": "2026-10-04T12:00:05.000000+00:00"
  }
}
```

锁定成功时**版本 +1**，锁定对强制出现在 `result.pairs` 中（即使全场
配对数因此下降）。重复锁定同一对（含颠倒顺序）视为**重传**：返回
`200`，`version` 不变。

#### `POST /drills/{drill_id}/locks/unlock` —— 解除锁定

```bash
curl -X POST http://127.0.0.1:8080/drills/DRILL-2026/locks/unlock \
  -H 'Content-Type: application/json' \
  -d '{"record_a": "R2", "record_b": "R1"}'
```

解除成功响应结构同上（`lock` 为 `null`、`locks` 为解除后的清单），
全场按无此锁定重算且**版本 +1**。锁定不存在返回
`404 lock_not_found`。

#### `GET /drills/{drill_id}/locks` —— 查询锁定清单及最新结果

```json
{
  "drill_id": "DRILL-2026",
  "locks": [
    {"record_a": "R1", "record_b": "R2",
     "time_diff_seconds": 5.0,
     "locked_at": "2026-10-04T12:00:05.000000+00:00"}
  ],
  "result": { "...": "与 GET /result 完全一致的最新互证结果" }
}
```

锁定操作的校验规则与错误见下节错误表。

### 5.5 版本快照（按版本回看 / 两版比较）

#### `GET /drills/{drill_id}/snapshots` —— 列出已留存快照的版本号

```bash
curl http://127.0.0.1:8080/drills/DRILL-2026/snapshots
```

```json
{ "drill_id": "DRILL-2026", "versions": [0, 1, 2, 3] }
```

0 版在创建演练时即存在；升级前的旧版本不在此清单中（不可回看）。

#### `GET /drills/{drill_id}/snapshots/{version}` —— 取回指定版本快照

```bash
curl http://127.0.0.1:8080/drills/DRILL-2026/snapshots/2
```

```json
{
  "drill_id": "DRILL-2026",
  "version": 2,
  "sealed": false,
  "pairs": [
    {"record_a": "R1", "record_b": "R2", "time_diff_seconds": 5.0}
  ],
  "unpaired": ["R3"],
  "total_time_diff_seconds": 5.0,
  "locks": [
    {"record_a": "R1", "record_b": "R2",
     "time_diff_seconds": 5.0,
     "locked_at": "2026-10-04T12:00:05.000000+00:00"}
  ],
  "quarantine": [
    {"record_no": "R9",
     "quarantined_at": "2026-10-04T12:00:06.000000+00:00"}
  ],
  "snapshot_at": "2026-10-04T12:00:05.234567+00:00"
}
```

- `pairs` / `unpaired` / `locks` / `quarantine` 为该版**留存时**的内容，
  之后不再改变（升级前留存的旧快照隔离清单为空）；
- `sealed` 是唯一的现态字段：封存后回看任意版本都显示 `true`；
- 未知演练 `404 drill_not_found`；版本未留存 `404 snapshot_not_found`。

#### `GET /drills/{drill_id}/snapshots/{from}/diff/{to}` —— 比较两个已留存版本

按记录号**稳定排序**列出配对、未配对记录与锁定对的新增/移除，
**同一记录对的时差变化单列**：

```bash
curl http://127.0.0.1:8080/drills/DRILL-2026/snapshots/1/diff/3
```

```json
{
  "drill_id": "DRILL-2026",
  "from_version": 1,
  "to_version": 3,
  "pairs": {
    "added":   [{"record_a": "R3", "record_b": "R6", "time_diff_seconds": 8.0}],
    "removed": [],
    "time_diff_changed": [
      {"record_a": "R1", "record_b": "R2",
       "from_time_diff_seconds": 5.0, "to_time_diff_seconds": 9.0}
    ]
  },
  "unpaired": {
    "added": ["R7"],
    "removed": ["R3", "R6"]
  },
  "locks": {
    "added":   [{"record_a": "R1", "record_b": "R2",
                 "time_diff_seconds": 9.0,
                 "locked_at": "2026-10-04T12:00:05.000000+00:00"}],
    "removed": []
  },
  "quarantine": {
    "added":   [{"record_no": "R9",
                 "quarantined_at": "2026-10-04T12:00:06.000000+00:00"}],
    "removed": []
  },
  "total_time_diff_seconds": {"from": 5.0, "to": 17.0, "delta": 12.0}
}
```

- 配对以 `(record_a, record_b)` 身份比较：`added` 取 **to（新）版**内容，
  `removed` 取 **from（旧）版**内容；两版都在但时差不同的对进入
  `pairs.time_diff_changed`，不重复计入新增/移除；
- `unpaired.added/removed` 为记录号集合差异，按记录号升序；
- `locks.added/removed` 按锁定对身份比较（锁定对仅时差变化时不出现，
  其时差变化已在 `pairs.time_diff_changed` 体现），列表按记录对升序；
- `quarantine.added/removed` 按记录号身份比较，列表按记录号升序；
- `total_time_diff_seconds` 给出两版总时差及 `delta`（to − from）；
- **相同版本**比较合法，各差异节均为空、`delta` 为 0（该版本仍须已留存）；
- 两个版本都必须已留存，否则 `404 snapshot_not_found`；未知演练
  `404 drill_not_found`。比较为只读，不改变任何当前状态。

### 5.6 批量写入预演 / 确认（复核员提交前预演）

复核员在**正式提交批量日志前**可先“预演”：请求体与
`POST /drills/{drill_id}/logs` **完全相同**，服务在只读事务内把整批
写入在内存中模拟一遍，返回**当前版本**、**拟写入后的完整互证结果**、
以及配对增删、同对时差变化、未配对增删。预演沿用与正式写入完全相同的
规范化、整批校验（非法记录、批内记录号重复）、锁定有效性与全场最优
配对规则；**预演不改变日志、锁定、结果、版本与快照**。

#### `POST /drills/{drill_id}/logs/preview` —— 预演（只读）

```bash
curl -X POST http://127.0.0.1:8080/drills/DRILL-2026/logs/preview \
  -H 'Content-Type: application/json' \
  -d '{"logs": [ { ... }, { ... } ]}'
```

响应 `200`：

```json
{
  "drill_id": "DRILL-2026",
  "base_version": 3,
  "current_result": { "...": "预演时的当前互证结果（同 GET /result）" },
  "proposed_version": 4,
  "would_advance_version": true,
  "proposed_result": {
    "drill_id": "DRILL-2026",
    "version": 4,
    "sealed": false,
    "pairs": [
      {"record_a": "R1", "record_b": "R2", "time_diff_seconds": 5.0}
    ],
    "unpaired": ["R3"],
    "total_time_diff_seconds": 5.0,
    "recomputed_at": null
  },
  "diff": {
    "pairs": {
      "added":   [{"record_a": "R1", "record_b": "R2", "time_diff_seconds": 5.0}],
      "removed": [],
      "time_diff_changed": [
        {"record_a": "R3", "record_b": "R6",
         "from_time_diff_seconds": 8.0, "to_time_diff_seconds": 3.0}
      ]
    },
    "unpaired": { "added": ["R3"], "removed": [] },
    "total_time_diff_seconds": { "from": 0.0, "to": 5.0, "delta": 5.0 }
  },
  "normalized_batch": [
    {"record_no": "R1", "station": "BH1AAA", "counterpart": "BH2BBB",
     "time": "2026-10-04T00:00:00+00:00", "band": "40M", "mode": "SSB"}
  ],
  "token": "pv1.<不透明字符串>"
}
```

- `base_version`：预演所基于的演练**当前版本**；
- `current_result`：预演时的现态结果；`proposed_result`：该批正式写入
  并全场重算后的完整结果（预演不落库，故无 `recomputed_at`）；
- `would_advance_version`：批次是否含新增/异内容更正。**同内容重传为
  `false`**，此时 `proposed_version == base_version`、各差异节为空；
- `diff.pairs`：与版本比较同一口径——配对按记录对身份比较，
  `added`/`removed` 分别取拟写入/当前内容，两版都在但时差不同的对进入
  `time_diff_changed`；`diff.unpaired` 为未配对记录号集合差异（升序）；
  `total_time_diff_seconds.delta = to - from`；
- `normalized_batch`：服务端规范化（呼号/波段/模式大写、时间换算 UTC）
  后的批次，确认时以它为准；
- `token`：**校验标识**，绑定“演练 + 基准版本 + 规范化批次”（无状态
  HMAC，密钥存于库内 `meta` 表，服务重启后既有演练仍可确认）。预演不
  在服务端留存任何会话，标识仅一次性对应一次确认窗口。

预演的整批报错与正式批量写入完全一致：非法记录 `422`、批内记录号重复
`409 duplicate_record_in_batch`、批次使任一既有锁定失效
`409 lock_invalidated`、演练已封存 `409 drill_sealed`、未知演练
`404 drill_not_found`；报错同样不改变任何状态。

#### `POST /drills/{drill_id}/logs/confirm` —— 按预演确认提交

把**原批次**连同预演返回的 `base_version`、`token` 原样回传：

```bash
curl -X POST http://127.0.0.1:8080/drills/DRILL-2026/logs/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "logs": [ { ... }, { ... } ],
    "base_version": 3,
    "token": "pv1.<预演返回的不透明字符串>"
  }'
```

- 服务在写事务内核验：演练未封存 → 当前版本仍为 `base_version` →
  标识与“演练 + 基准版本 + 规范化批次”一致；
- **标识不匹配**（批次内容/顺序/规范化结果被改动、标识伪造、跨演练
  使用）返回 `409 preview_token_mismatch`；**版本已变化**（预演后有
  其他写入/锁定/解锁推进了版本）返回
  `409 preview_base_version_changed`；**演练在预演后被封存**返回
  `409 drill_sealed`。三种拒绝都**不改变任何状态**，需重新预演；
- 校验通过后**完全按现有批量写入语义在一个原子事务内落库**：整批
  upsert、有变化才全场重算并 `version + 1`、同内容重传不推进版本、
  不产生额外的锁定/快照语义；响应与 `POST .../logs` 完全一致
  （`ResultResponse`），因此正式确认的结果必然等于预演的
  `proposed_result`（`recomputed_at` 除外）。

### 5.7 未互证日志缺口诊断（复核员查看未配对缺口）

#### `GET /drills/{drill_id}/gap-diagnostics` —— 未配对日志缺口诊断（只读）

按演练当前日志，为当前互证结果中的**每条未配对记录**依次归因缺口原因。

```bash
curl http://127.0.0.1:8080/drills/DRILL-2026/gap-diagnostics
```

响应 `200`：

```json
{
  "drill_id": "DRILL-2026",
  "version": 3,
  "sealed": false,
  "allowed_time_diff_seconds": 10.0,
  "gaps": [
    {
      "record_no": "M1",
      "reason": "band_or_mode_mismatch",
      "reason_label": "波段或模式不符",
      "counterpart_record_no": "M2",
      "time_diff_seconds": 0.0
    },
    {
      "record_no": "T1",
      "reason": "time_diff_exceeded",
      "reason_label": "时差超限",
      "counterpart_record_no": "T2",
      "time_diff_seconds": 15.0
    },
    {
      "record_no": "X2",
      "reason": "candidate_occupied",
      "reason_label": "候选已占用",
      "counterpart_record_no": "Y1",
      "time_diff_seconds": 1.0
    },
    {
      "record_no": "Z1",
      "reason": "missing_counterpart_log",
      "reason_label": "缺少对方日志",
      "counterpart_record_no": null,
      "time_diff_seconds": null
    }
  ]
}
```

字段与归因规则：

- `gaps` 只含当前结果中的未配对记录，**按记录号升序**；
- 每条按下列顺序对**当前日志**判定，命中前者即归为该类：

  | `reason` | 中文（`reason_label`） | 判定条件 |
  | --- | --- | --- |
  | `candidate_occupied` | 候选已占用 | 有完全符合互指、波段、模式、时差条件的对方候选却未入选（该候选必已被别的配对/锁定占用） |
  | `time_diff_exceeded` | 时差超限 | 否则有互指且波段、模式相同的记录（时差均超允许值） |
  | `band_or_mode_mismatch` | 波段或模式不符 | 否则有互指记录（波段或模式不一致） |
  | `missing_counterpart_log` | 缺少对方日志 | 没有任何互指记录 |

- 前三类的 `counterpart_record_no` / `time_diff_seconds` 取**该类候选中
  时差最小、再按对方记录号最小**的一条及其与该未配对记录的实际时差；
  `missing_counterpart_log` 两者均为 `null`；
- `version` / `sealed` 为诊断时的**当前现态**：查询本身不推进版本，
  写入、更正、锁定/解除后再查即反映新结果与新版本；
- **被隔离记录**不出现在 `gaps` 中，也**不作为其他记录的诊断候选**
  （候选仅限未隔离日志）；
- 诊断为**只读操作**，不改变日志、结果、版本与快照；**封存后仍返回
  `200`**（`sealed` 为 `true`）；未知演练返回 `404 drill_not_found`；
- 演练尚无任何日志（空场）时 `gaps` 为空列表。

### 5.8 按台站汇总（演练结果台站统计）

#### `GET /drills/{drill_id}/station-summary` —— 按台站汇总互证结果（只读）

按演练读取**当前日志与互证结果**，以每条日志的本台呼号归组，逐台站
返回日志总数、已配对数、未配对数与配对覆盖率。

```bash
curl http://127.0.0.1:8080/drills/DRILL-2026/station-summary
```

响应 `200`：

```json
{
  "drill_id": "DRILL-2026",
  "version": 3,
  "sealed": false,
  "stations": [
    {
      "station": "BH1AAA",
      "total_logs": 2,
      "paired_logs": 1,
      "unpaired_logs": 1,
      "paired_coverage": 0.5
    },
    {
      "station": "BH2BBB",
      "total_logs": 1,
      "paired_logs": 1,
      "unpaired_logs": 0,
      "paired_coverage": 1.0
    },
    {
      "station": "BH3CCC",
      "total_logs": 1,
      "paired_logs": 0,
      "unpaired_logs": 1,
      "paired_coverage": 0.0
    }
  ]
}
```

字段与统计口径：

- `stations` 按台站呼号**升序**；只有**实际上报过日志**的台站才出现
  （仅在对方呼号中被指认、自身未上报的台站不产生汇总项）；
- **被隔离记录不计入**台站的日志总数、已配对数与未配对数（日志全部被
  隔离的台站不产生汇总项）；恢复后按当前日志重新汇总；
- `total_logs`：该台站上报的日志总数；`paired_logs`：其中进入互证
  配对的条数——**一对互证的两条记录分别计入两端台站**；
  `unpaired_logs`：其中未配对的条数——**未配对记录只计入其本台**；
  恒有 `total_logs = paired_logs + unpaired_logs`；
- `paired_coverage`：配对覆盖率 = `paired_logs / total_logs`
  （0..1，保留 6 位小数，如 `0.333333`）；
- `version` / `sealed` 为查询时的**当前现态**：查询本身不推进版本，
  写入、更正、锁定/解除后再查即反映新结果与新版本；
- 汇总为**只读操作**，不改变日志、结果、版本与快照；**封存后仍返回
  `200`**（`sealed` 为 `true`）；未知演练返回 `404 drill_not_found`；
- 演练尚无任何日志（空场）时 `stations` 为空列表。

### 5.9 日志归错演练：跨演练迁移（复核员一次性改派）

#### `POST /drills/{drill_id}/transfer` —— 把记录从源演练原子迁移到目标演练

路径上的 `drill_id` 为**源演练**，请求体给出**目标演练号**与
**记录号列表**（非空、不可重复）。一次调用只涉及这两个演练：
两者各推进一次版本、各重算一次、各留存一份新版本快照，全部在
**同一事务**原子提交；校验不过则整次拒绝、两端状态不变。

```bash
curl -X POST http://127.0.0.1:8080/drills/SRC-2026/transfer \
  -H 'Content-Type: application/json' \
  -d '{"target_drill_id": "DST-2026", "record_nos": ["R1", "R2"]}'
```

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `target_drill_id` | string | 目标演练号（字母/数字/`_`/`-`/`.`），须与源演练不同且已存在、未封存 |
| `record_nos` | string[] | 待迁移记录号列表，**至少 1 条**，服务端 `strip` 后不可为空、不可重复 |

受理条件（任一不满足即拒绝，见第 6 节错误码）：两演练均存在且未封存、
源 ≠ 目标、源记录全部存在、目标无同号记录、被移记录不参与源演练锁定、
被移记录未处于隔离状态。

响应 `200`：

```json
{
  "source_drill_id": "SRC-2026",
  "target_drill_id": "DST-2026",
  "moved_record_nos": ["R1", "R2"],
  "source_version": 4,
  "target_version": 2,
  "source_result": {
    "drill_id": "SRC-2026",
    "version": 4,
    "sealed": false,
    "pairs": [],
    "unpaired": ["R7"],
    "total_time_diff_seconds": 0.0,
    "recomputed_at": "2026-10-06T01:00:00.000000+00:00"
  },
  "target_result": {
    "drill_id": "DST-2026",
    "version": 2,
    "sealed": false,
    "pairs": [
      {"record_a": "R1", "record_b": "R2", "time_diff_seconds": 5.0}
    ],
    "unpaired": [],
    "total_time_diff_seconds": 5.0,
    "recomputed_at": "2026-10-04T12:00:05.123456+00:00"
  }
}
```

- `moved_record_nos`：实际迁移的记录号，按请求顺序回显（已去空白、
  去重）；
- `source_version` / `target_version`：迁移后两端的**新版本号**，
  分别等于迁移前版本 `+1`（两端各只推进一次）；
- `source_result` / `target_result`：两端**重算后的完整互证结果**，
  结构与 `GET /result` 完全一致；目标端按**目标演练的允许时差**
  重新配对（同两条记录在源端因时差超限未配对，迁入更宽的目标演练后
  可能配上）；
- 被移日志的呼号/波段/模式、UTC 时间与 `updated_at` **原样保留**；
  源端锁定中**未涉及的记录锁定照常生效**，目标端既有锁定不变；
  **历史快照不被改写**，仅新增两端各自的新版本快照；
- 拒绝时不返回结果体，统一错误信封
  `{"error": "...", "message": "..."}`，两端日志、版本、结果、
  快照与锁定均保持迁移前状态。

### 5.10 争议日志隔离（复核员批量隔离 / 恢复）

记录号通过请求体传递（避免 `/`、`.` 等记录号字符进入路径）。
隔离只改变互证资格：不删除日志、不改写原字段；被隔离记录不参与
配对、不进入未配对清单与缺口诊断（候选也仅限未隔离日志），不计入
台站汇总。隔离期间仍可按原接口更正日志，隔离状态保留。

#### `PUT /drills/{drill_id}/quarantine` —— 批量隔离争议日志

```bash
curl -X PUT http://127.0.0.1:8080/drills/DRILL-2026/quarantine \
  -H 'Content-Type: application/json' \
  -d '{"record_nos": ["R3", "R5"]}'
```

响应 `200`：

```json
{
  "drill_id": "DRILL-2026",
  "affected_record_nos": ["R3", "R5"],
  "quarantine": [
    {"record_no": "R3",
     "quarantined_at": "2026-10-06T01:00:00.000000+00:00"},
    {"record_no": "R5",
     "quarantined_at": "2026-10-06T01:00:00.000000+00:00"}
  ],
  "result": {
    "drill_id": "DRILL-2026",
    "version": 4,
    "sealed": false,
    "pairs": [{"record_a": "R1", "record_b": "R2", "time_diff_seconds": 5.0}],
    "unpaired": ["R4"],
    "total_time_diff_seconds": 5.0,
    "recomputed_at": "2026-10-06T01:00:00.000000+00:00"
  }
}
```

- `affected_record_nos`：本次**实际**隔离的记录号（请求顺序）；批次中
  已隔离的记录自动跳过。**有实际变化时版本只 +1**（整批一次），结果与
  隔离清单一并写入新版本快照；整批均为重复隔离时 `affected_record_nos`
  为空、**版本不推进**；
- `quarantine`：操作后的**完整隔离清单**（按记录号升序）；
- `result`：重算后的最新互证结果（同 `GET /result`）。

#### `POST /drills/{drill_id}/quarantine/restore` —— 批量恢复被隔离日志

```bash
curl -X POST http://127.0.0.1:8080/drills/DRILL-2026/quarantine/restore \
  -H 'Content-Type: application/json' \
  -d '{"record_nos": ["R5"]}'
```

响应结构同上：`affected_record_nos` 为本次实际恢复的记录；恢复后
**按当前日志重新求解**全场互证（含隔离期间的更正），版本只 +1；
批次中未隔离的记录自动跳过，整批均为重复恢复时不推进版本。

#### `GET /drills/{drill_id}/quarantine` —— 查询隔离清单（只读）

```bash
curl http://127.0.0.1:8080/drills/DRILL-2026/quarantine
```

```json
{
  "drill_id": "DRILL-2026",
  "quarantine": [
    {"record_no": "R3",
     "quarantined_at": "2026-10-06T01:00:00.000000+00:00"}
  ],
  "result": { "...": "与 GET /result 完全一致的最新互证结果" }
}
```

校验与错误（均整批拒绝、状态不变，见第 6 节）：记录不存在
`404 quarantine_record_missing`、批次内编号重复
`409 duplicate_record_in_quarantine`、涉及人工锁定中的记录
`409 quarantine_record_locked`、演练已封存 `409 drill_sealed`、
未知演练 `404 drill_not_found`、记录号列表为空 `422`。
关联约束：锁定被隔离记录 `409 lock_record_quarantined`、迁移被隔离
记录 `409 transfer_record_quarantined`。封存后仍可查询隔离清单。

### 5.11 复核员批量裁决（锁定/解除与隔离/恢复一次提交）

复核员把**相互牵连的裁决**一次提交：路径给出**演练号**，请求体给出
**基准版本**与两份意图列表——**锁定/解除记录对**、**隔离/恢复记录**。
整批在**单个事务**内按**最终状态**判定：意图顺序不影响结果；校验
不过则整批拒绝、状态不变；有实际状态变化时整批只推进一次版本。

#### `POST /drills/{drill_id}/adjudications` —— 提交批量裁决

```bash
curl -X POST http://127.0.0.1:8080/drills/DRILL-2026/adjudications \
  -H 'Content-Type: application/json' \
  -d '{
    "base_version": 3,
    "locks": [
      {"action": "unlock", "record_a": "R1", "record_b": "R2"},
      {"action": "lock",   "record_a": "R3", "record_b": "R4"}
    ],
    "quarantine": [
      {"action": "quarantine", "record_no": "R2"},
      {"action": "restore",    "record_no": "R5"}
    ]
  }'
```

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `base_version` | int | 基准版本（≥0）：演练当前版本须仍为该版本，否则 `409 adjudication_base_version_changed` |
| `locks` | array | 锁定/解除意图列表（可缺省为空）：`action` 取 `lock`/`unlock`，`record_a`/`record_b` 顺序任意（服务端内部升序） |
| `quarantine` | array | 隔离/恢复意图列表（可缺省为空）：`action` 取 `quarantine`/`restore`，`record_no` 为单条记录号 |

`locks` 与 `quarantine` 至少提交一条意图（全空 `422`）；记录号
`strip` 后不能为空；同一锁定意图的两个记录号相同返回
`422 lock_same_record`。

判定规则（全部满足才受理，任一不满足**整批拒绝、状态不变**）：

1. 演练存在且未封存，当前版本等于 `base_version`；
2. **重复同向意图只算一次**；同一记录对既锁定又解除、同一记录既隔离
   又恢复 => `409 adjudication_intent_conflict`；
3. 任一意图**引用不存在的记录** => `404 adjudication_record_missing`；
4. **最终锁定对**（现有锁定 − 解除 + 新锁）须全部满足既有互证候选
   条件（现存/互指/波段/模式/时差）=> 否则 `409 lock_not_candidate`；
   **同一记录被多对锁定** => `409 record_already_locked`；最终锁定对
   成员**仍处于隔离状态** => `409 lock_record_quarantined`；
5. 按最终状态合成，因此**同批解除旧锁并隔离、恢复并锁定均可**；
   解除本不存在的锁定、恢复未隔离的记录为幂等空操作。

响应 `200`：

```json
{
  "drill_id": "DRILL-2026",
  "base_version": 3,
  "version": 4,
  "version_advanced": true,
  "applied": {
    "locked":   [{"record_a": "R3", "record_b": "R4"}],
    "unlocked": [{"record_a": "R1", "record_b": "R2"}],
    "quarantined": ["R2"],
    "restored": ["R5"]
  },
  "locks": [
    {"record_a": "R3", "record_b": "R4",
     "time_diff_seconds": 8.0,
     "locked_at": "2026-10-06T01:00:00.000000+00:00"}
  ],
  "quarantine": [
    {"record_no": "R2",
     "quarantined_at": "2026-10-06T01:00:00.000000+00:00"}
  ],
  "result": {
    "drill_id": "DRILL-2026",
    "version": 4,
    "sealed": false,
    "pairs": [{"record_a": "R3", "record_b": "R4", "time_diff_seconds": 8.0}],
    "unpaired": ["R1", "R5"],
    "total_time_diff_seconds": 8.0,
    "recomputed_at": "2026-10-06T01:00:00.000000+00:00"
  }
}
```

- `version`：当前版本。有实际状态变化时 = `base_version + 1`（整批
  只推进一次），重算结果与锁定/隔离清单在**同一事务**写入新版本
  快照；`version_advanced` 标识本批是否推进了版本；
- `applied`：本批**实际生效**的锁定/解除/隔离/恢复（请求顺序、已
  去重）；已处于目标状态的意图自动跳过不出现在其中；
- **纯重传**（全部意图均已是目标状态）：`version_advanced` 为
  `false`、`version` 不变、`applied` 各项为空、不产生新快照；
- `locks` / `quarantine`：操作后的**完整锁定清单**（按记录对升序）
  与**完整隔离清单**（按记录号升序）；`result` 与 `GET /result`
  完全一致的完整互证结果；
- 既有单项锁定/隔离接口及响应契约保持兼容：批量裁决推进的版本
  同样留存快照、可回看可比较，也作为后续预演/确认的基准版本。

---

## 6. 错误响应与状态码

业务错误统一形如 `{"error": "<错误码>", "message": "<中文说明>"}`；
请求体校验错误为 FastAPI 标准 `422`（`detail` 中含明确中文原因）。

| 场景 | 状态码 | 错误码 / 说明 |
| --- | --- | --- |
| 允许时差为负数 | `422` | `允许时差必须为非负数（秒）` |
| 时间缺时区信息 | `422` | `时间必须携带时区信息，例如 2026-10-04T08:00:00+08:00` |
| 本台呼号与对方呼号相同 | `422` | `本台呼号与对方呼号不能相同` |
| 记录号/呼号/波段/模式为空 | `422` | 对应字段不能为空 |
| 未知演练（查询/写入/封存/锁定/缺口诊断/台站汇总/隔离/批量裁决） | `404` | `drill_not_found` |
| 演练号已存在 | `409` | `drill_exists` |
| 写入/封存/锁定/隔离/恢复/批量裁决已封存演练 | `409` | `drill_sealed`（封存后查询仍 `200`） |
| 批次内记录号重复 | `409` | `duplicate_record_in_batch` |
| 锁定的两个记录号相同 | `422` | `lock_same_record` |
| 锁定引用了不存在的记录 | `404` | `lock_record_missing` |
| 两条日志未相互指认 / 波段或模式不一致 / 时差超限 | `409` | `lock_not_candidate` |
| 锁定涉及的记录已被其他锁定占用 | `409` | `record_already_locked` |
| 解除不存在的锁定 | `404` | `lock_not_found` |
| 写入/更正使任一既有锁定失效 | `409` | `lock_invalidated`（整次写入拒绝、状态不变） |
| 确认时校验标识与演练/基准版本/规范化批次不符 | `409` | `preview_token_mismatch`（拒绝且不改状态） |
| 确认时演练版本已不同于预演基准版本 | `409` | `preview_base_version_changed`（拒绝且不改状态） |
| 取回/比较未留存版本（含升级前旧版本） | `404` | `snapshot_not_found` |
| 跨演练迁移的源与目标演练相同 | `422` | `same_drill_transfer` |
| 跨演练迁移记录号列表为空（列表至少 1 条） | `422` | 请求体校验（`record_nos` 不能为空） |
| 跨演练迁移记录号列表内重复 | `409` | `duplicate_record_in_transfer` |
| 待迁移记录在源演练中不存在 | `404` | `transfer_record_missing` |
| 目标演练已存在同号记录 | `409` | `target_record_exists` |
| 被移记录参与了源演练的人工锁定 | `409` | `transfer_record_locked`（整次拒绝、两端状态不变；请先解除该锁定） |
| 隔离/恢复批次中记录不存在 | `404` | `quarantine_record_missing`（整批拒绝、状态不变） |
| 隔离/恢复批次内记录号重复 | `409` | `duplicate_record_in_quarantine` |
| 隔离/恢复批次涉及人工锁定中的记录 | `409` | `quarantine_record_locked`（已有锁定成员不得隔离，请先解除锁定） |
| 锁定被隔离的记录 | `409` | `lock_record_quarantined`（隔离记录不得人工锁定，请先恢复） |
| 迁移被隔离的记录 | `409` | `transfer_record_quarantined`（整次拒绝、两端状态不变；请先恢复） |
| 批量裁决中同一对象出现相反意图 | `409` | `adjudication_intent_conflict`（同一记录对既锁定又解除 / 同一记录既隔离又恢复，整批拒绝） |
| 批量裁决基准版本与当前版本不一致 | `409` | `adjudication_base_version_changed`（基准版本过期，整批拒绝、状态不变） |
| 批量裁决引用不存在的记录 | `404` | `adjudication_record_missing`（整批拒绝、状态不变） |
| 批量裁决最终锁定对不满足互证候选条件 | `409` | `lock_not_candidate`（整批拒绝） |
| 批量裁决最终状态同一记录被多对锁定 | `409` | `record_already_locked`（整批拒绝） |
| 批量裁决最终锁定对成员仍处于隔离状态 | `409` | `lock_record_quarantined`（整批拒绝；可同批解除旧锁再隔离，或恢复再锁定） |

---

## 7. 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由、异常处理、lifespan 初始化
│   ├── schemas.py       # 请求/响应模型与输入校验（时区/负值/同呼号）
│   ├── database.py      # SQLite 持久化、事务、版本/封存/锁定/隔离/快照/批量预演
│   │                    #   与按台站汇总、跨演练日志迁移（两端原子提交）、复核员批量裁决
│   ├── matching.py      # 配对核心算法（二分图最小费用最大流，支持强制锁定）
│   │                    #   与未配对缺口诊断（diagnose_gaps）
│   └── config.py        # 环境变量配置
├── scripts/
│   ├── init_db.py       # 数据初始化（建库/建表/WAL/旧库快照回填，幂等）
│   └── demo.py          # 端到端调用演示（含版本回看/比较、缺口诊断、
│                        #   台站汇总、批量裁决，纯标准库）
├── tests/               # 算法单测（含穷举对照）+ HTTP 集成测试（含快照）
├── examples/requests.http
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

### 并发与持久化说明

- SQLite 启用 **WAL**：写入串行的同时允许并发读取，封存/重算期间
  结果查询不被阻塞；
- 每个写请求使用独立连接执行 `BEGIN IMMEDIATE`，配合 `busy_timeout=30s`
  排队获取写锁；`version`、日志变更、配对结果在同一事务提交；
- 集成测试 `tests/test_api.py::test_concurrent_writes_are_serialized`
  以 20 个并发线程验证最终版本严格为 20、无丢失更新。
