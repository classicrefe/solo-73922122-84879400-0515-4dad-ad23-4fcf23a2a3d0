# 高程瓦片供数服务

面向坡度分析的高程供数 API。矩形底图按**行列编号**划分瓦片，每块 **17×17 个整数高程点**，相邻块**重复存储共边**上的点，因此跨瓦片取点（如坡度计算）无需额外拼接逻辑。数据员**按块暂存**修测，再**点名一块发版**：服务自动选出**换块最少且所有共边逐点吻合**的方案整批生效；无解时报告冲突边两侧瓦片坐标，现行底图与暂存原样保留。

内置**质检隔离**：质检员取窗拿到原高程与取数标记，带标记提交窗内坏点（全图坐标）后，坏点在窗口取数 API 中返回 `null`（坡度作业自动跳过）；只有该点高程随发版**实际改变**才解除隔离、恢复供数。

- 技术栈：FastAPI + SQLite（WAL），Docker Compose 一键启动
- 交互式文档：启动后访问 `http://localhost:8000/docs`

## 快速开始

```bash
# Docker（推荐）
docker compose up --build

# 或本地运行
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库文件路径由环境变量 `ELEV_DB_PATH` 指定（默认 `./data/elev.db`，Compose 中为 `/data/elev.db` 并挂载卷持久化）。

```bash
# 运行测试
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 业务流程与 API

| 步骤 | 方法与路径 | 说明 |
|---|---|---|
| 录入底图 | `POST /basemap` | 两种方式二选一：`tiles` 逐块给出（校验所有共边，失配则 422 并列出失配边与点）；或 `grid` 给出 (16·rows+1)×(16·cols+1) 整图，自动切块、接缝天然吻合 |
| 底图概况 | `GET /basemap` | 行列数、各块现行版本号、各块暂存数量 |
| 暂存修测 | `POST /tiles/{row}/{col}/staged` | 可多次暂存同一块，发版时取 id 最大（最新）的一版 |
| 查看暂存 | `GET /tiles/{row}/{col}/staged`、`GET /staged` | 暂存版本列表 |
| 点名发版 | `POST /publish` | 请求体 `{"row": r, "col": c}`；成功返回换版坐标与清除的暂存 id；冲突返回 409 与冲突边两侧瓦片坐标 |
| 整图取数 | `GET /basemap/grid` | 拼接后的完整高程面 + 各块版本号 |
| 跨瓦片取点 | `GET /basemap/window?row0=&col0=&row1=&col1=` | 整图坐标系下的矩形窗口（含端点）；**隔离点返回 `null`** |
| 单块取数 | `GET /tiles/{row}/{col}` | 含现行版本号 |
| 质检取窗 | `POST /qc/windows` | 返回窗内**原高程**（不遮蔽隔离点）与**取数标记**；标记只存窗口范围与取数时刻的变更序号，不另存窗口副本 |
| 提交坏点 | `POST /qc/isolations` | 带标记提交窗内坏点的全图坐标；窗内任一点自取数后被改过（哪怕改回原值）则**整单退回**（409 `window_stale`） |
| 隔离点列表 | `GET /qc/isolations` | 当前生效的隔离点（全图坐标） |
| 重置 | `DELETE /basemap` | 删除底图、全部暂存与全部质检状态（重录/演示用）；**重录后旧取数标记失效** |

坐标均从 0 开始。瓦片行列数上限 64×64（防止整图响应过大）。

### 发版语义

- **点名块必须**换上其最新暂存版（没有暂存版则 409 `no_staged_version`）；
- 其余每块可**保留现行版**或换上**各自最新暂存版**；
- 约束：发版后所有共边 17 个点逐点相等；目标：**换版块数最少**；
- 成功：整批原子生效，返回 `changed_tiles`（换版坐标与新版本号）与
  `cleared_staged_ids`；**只清除本次用到的暂存**（未涉及的、以及同块的
  更旧暂存版都保留）；同时返回 `lifted_isolations`——本次发版中因高程
  实际改变而解除隔离的坏点；
- 无解：409 `publish_conflict`，`conflicting_edges` 列出冲突边两侧瓦片
  坐标，**现行底图、暂存与质检状态均未改动**。

### 质检隔离语义

- **取窗**：`POST /qc/windows` 返回窗内**原高程**（隔离点也不遮蔽，质检
  需要看到真值）与**取数标记**。标记持久化在 SQLite，只记录窗口范围与
  取数时刻的全局变更序号，**不另存窗口高程副本**；**重启后仍可用**，
  **重录底图后旧标记失效**（重置会清空全部质检状态）。
- **提交**：`POST /qc/isolations` 带标记提交**窗内**坏点的**全图坐标**
  （出窗 422 `points_out_of_window`）。窗口内**任一点**自取数后被改过
  ——**即使改回原值**——整单退回（409 `window_stale`，响应列出被改过的
  点），一个点也不隔离；只改窗口外点的发版不影响提交。判定依据：每次
  成功发版把全局变更序号 +1，并逐点记录"高程实际改变"的格点序号
  （`point_changes` 表），与取数时刻的序号比较。
- **隔离**：生效的坏点在 `GET /basemap/window` 中返回 `null`（坡度作业
  取窗自动跳过）；`GET /basemap/grid`、`GET /tiles/{r}/{c}` 与质检取窗
  仍返回原值，供修测与核对使用。隔离按**全图坐标**唯一——共边上的同一
  格点只占一条记录、按一点处理；**重复标记同一读数只隔离一次**（同一
  提交内重复、跨提交重复都算，响应在 `already_isolated` 中列出）。
- **解除**：只有该点高程**随发版实际改变**才解除隔离（发版换上与现行
  完全一致的版本不解除）；**失败的发版整体回滚**，隔离保持原状态。
  解除与换版在同一事务内生效，取窗永远读到一致的组合。

### 演示：隔离坏点 → 修测后恢复供数

启动后访问 `http://localhost:8000/docs`，依次执行（均可在 Swagger UI
中直接操作）：

```bash
# 1. 录入 1×1 底图（全图高程 100）
curl -X POST localhost:8000/basemap -H 'Content-Type: application/json' \
  -d "{\"grid\": $(python -c 'import json;print(json.dumps([[100]*17]*17))')}"

# 2. 质检取窗：拿到原高程与取数标记
curl -X POST localhost:8000/qc/windows -H 'Content-Type: application/json' \
  -d '{"row0":0,"col0":0,"row1":16,"col1":16}'
#   -> {"mark": "<取数标记>", "fetch_seq": 0, "grid": [[100, ...], ...]}

# 3. 带标记提交坏点（全图坐标 (3,3)）
curl -X POST localhost:8000/qc/isolations -H 'Content-Type: application/json' \
  -d '{"mark":"<取数标记>","points":[{"row":3,"col":3}]}'
#   -> {"newly_isolated": [[3,3]], "total_isolated": 1, ...}

# 4. 坡度作业取窗：隔离点返回 null
curl 'localhost:8000/basemap/window?row0=3&col0=3&row1=3&col1=3'
#   -> {"grid": [[null]], ...}

# 5. 修测：暂存一版把 (3,3) 改为 555 并发版
python - <<'EOF'
import json, urllib.request
t = [[100]*17 for _ in range(17)]; t[3][3] = 555
req = urllib.request.Request("http://localhost:8000/tiles/0/0/staged",
    data=json.dumps({"data": t}).encode(), headers={"Content-Type": "application/json"})
urllib.request.urlopen(req)
req = urllib.request.Request("http://localhost:8000/publish",
    data=b'{"row":0,"col":0}', headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())  # "lifted_isolations": [[3, 3]]
EOF

# 6. 恢复供数：隔离解除，窗口返回修测后的新值
curl 'localhost:8000/basemap/window?row0=3&col0=3&row1=3&col1=3'
#   -> {"grid": [[555]], ...}
```

### 示例

```bash
# 录入 1×2 底图（整图方式，全图高程 100）
curl -X POST localhost:8000/basemap -H 'Content-Type: application/json' \
  -d "{\"grid\": $(python -c 'import json;print(json.dumps([[100]*33]*17))')}"

# 暂存 (0,0)：右共边改为 200
python - <<'EOF'
import json, urllib.request
t = [[100]*17 for _ in range(17)]
for row in t: row[16] = 200
req = urllib.request.Request("http://localhost:8000/tiles/0/0/staged",
    data=json.dumps({"data": t}).encode(), headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
EOF

# 点名 (0,0) 发版：邻块 (0,1) 没有能接上的暂存版 -> 409 并报告冲突边
curl -X POST localhost:8000/publish -H 'Content-Type: application/json' -d '{"row":0,"col":0}'
```

## 发版算法（为什么"换块最少"是确定的）

对每条共边，记现行值为 K、两侧最新暂存版在该边上的值为 A、B（现行底图
本身接缝吻合，两侧 K 相同）：

| 情形 | 结论 |
|---|---|
| A=K 且 B=K | 无约束 |
| A≠K 且 B=K | 左侧永远不能换（换了右侧接不上） |
| A=K 且 B≠K | 右侧永远不能换 |
| A≠K、B≠K、A=B | **等值边**：两侧必须同进退 |
| A≠K、B≠K、A≠B | 两侧都不能换 |
| 一侧无暂存 | 另一侧仅当 A=K 时可换 |

把等值边做**并查集**连边，点名块所在连通分量就是唯一被强制换版的最小集合：
任何可行方案都必须整个换掉它，而分量之外全部保留现行版必然可行。对该分量
做全量共边校验：通过即为换块最少的方案（唯一）；不通过则不存在任何可行方案
（分量外的块即使换版也接不上，否则它早被等值边连进分量），校验发现的失配边
就是要报告的冲突边。算法复杂度 O(瓦片数 × 17)。实现见 `app/solver.py`。

## 并发与可串行化

暂存、发版、取数、质检操作并发时，结果与某种依次执行的顺序一致：

- SQLite **WAL** 模式：读者不阻塞写者，读事务看到一致性快照；
- 写操作（录入/暂存/发版/重置/质检取窗/提交坏点）一律 `BEGIN IMMEDIATE`：
  事务开始即拿写锁，写者之间严格串行，`busy_timeout=30s` 兜底等待；
- 发版的"读取现行版+暂存 → 规划 → 批量更新+清除暂存+逐点记录变更+解除
  隔离"在**同一个写事务**内完成，因此两个并发发版不会基于过期快照做决策，
  失败的发版整体回滚、质检状态原样保留；
- 窗口取数在**同一个只读事务**内读取高程与隔离状态，二者必然来自同一
  已提交状态——不会看到"值已改但隔离未解"或"隔离已解但值未改"的中间态；
  质检取窗的高程读取与标记签发同样在同一事务内完成。

`tests/test_concurrency.py` 用真实 uvicorn + 多线程验证：两个发版竞争同一
暂存版时恰好一个成功、另一个报 `no_staged_version`；发版 4×4 全图换版期间
并发整图取数，每次取到的都是完整旧图或完整新图；隔离解除与高程变更原子
可见（并发取窗只读到 `null` 或新值，绝不读到已解除隔离的旧值）；质检取窗/
提交与发版并发压力下无 5xx 且最终隔离状态自洽。

## 项目结构

```
app/
  main.py     FastAPI 入口与路由（/docs）
  service.py  业务层：事务边界、发版编排、质检隔离
  solver.py   发版规划器（纯函数）与接缝校验
  db.py       SQLite 连接、建表、WAL
  models.py   请求体模型（17×17 校验）
tests/        pytest：API、质检、规划器单测、并发测试
Dockerfile / docker-compose.yml
```

## 实测结果

- `python -m pytest tests/ -q`：**49 passed**（含 5 个并发用例，重复运行稳定）
- 本地 uvicorn 冒烟：录入 → 质检取窗（原高程+标记）→ 提交坏点 → 窗口
  取数返回 `null` → 暂存修测 → 发版（`lifted_isolations` 正确）→ 窗口
  恢复新值；取数后窗内点被改过（含改回原值）时提交整单退回 409；共边点
  重复提交只隔离一次；`/docs` 与 `/openapi.json` 正常，质检三端点可在
  Swagger UI 中直接操作

## 待完成项

- 本环境无 Docker，`Dockerfile`/`docker-compose.yml` 未经实际构建验证（已按
  标准实践编写，本地等效命令 `uvicorn app.main:app` 验证通过）
- 无认证与权限控制；暂存、发版、质检提交未记录操作人
- 仅支持单幅底图（重录需先 `DELETE /basemap`，会清空全部质检状态）
- 取数标记只增不减，长期运行需清理策略（如按 `created_at` 过期）
- 窗口取数与质检取窗目前整图拼接后切片，瓦片规模放大后可改为按需读取
  相交瓦片
- 发版逐点 diff 记录 `point_changes`，极端大图（64×64 块）全图换版时
  写入量较大，可按需优化为分块批量写
- 历史暂存版无自动清理策略（发版只清除用到的版本，旧版需人工管理）
- 瓦片尺寸 17×17 为常量（`app/solver.py: TILE`），未参数化
- 仅"供数"，未提供坡度计算端点（消费方自行基于整图/窗口接口计算）
