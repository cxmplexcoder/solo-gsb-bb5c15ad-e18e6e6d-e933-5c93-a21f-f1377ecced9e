# 实验室样品分装谱系 API

使用 **Python + FastAPI + SQLite** 实现的样品分装 / 谱系追踪服务。

登记带体积的**初始样品**后，可以**批量**提交一批容器间转移；系统按**源容器当时的混合比例**
传播其中**全部祖先来源**，所有体积运算使用标准库 [`decimal.Decimal`](https://docs.python.org/3/library/decimal.html)
完成，全程不经过 `float`，杜绝二进制浮点误差。

- 一批转移**严格依次发生**：后面的转移能看到同批中前面转移的结果。
- 整批是一个 SQLite 事务：**源液不足、未知容器、自转移**（或任何参数错误）都会导致
  **整批拒绝、状态完全不变**。
- 每个容器持有持久化 **version**（创建为 0，每被一批成功提交的转移触碰一次 +1）；
  批量转移可携带 `expected_versions` 做**乐观并发控制**：版本不符返回 `409 stale_container`
  并整批回滚。
- 可用 `POST /transfers/preview` **预演**同一批转移：与真实提交完全相同的校验与分摊规则，
  返回每一步的扣取明细与受影响容器的最终状态（含**预测提交后的版本**），但**绝不写入数据库**。
- 可查询任一容器的**当前体积**、各祖先初始样品的**来源占比**与当前**版本**。

---

## 一条命令启动

需要已安装 Docker 与 Docker Compose v2：

```bash
docker compose up --build
```

启动后服务监听：**http://localhost:8000**

- Swagger 交互文档：http://localhost:8000/docs
- OpenAPI 描述：http://localhost:8000/openapi.json
- 健康检查：http://localhost:8000/health

容器启动时（`docker-entrypoint.sh`）会：

1. 安装 Python 依赖（`requirements.txt`）；
2. 在 `DATABASE_PATH` 处创建 SQLite 数据库与表（已存在则保持不变，幂等）；
3. 启动 uvicorn。

SQLite 数据保存在 Docker 命名卷 `lineage-data`（容器内 `/data`）中，重启不丢失。

停止与清理：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时删除数据卷
```

---

## 配置项

全部配置通过环境变量注入（见 `docker-compose.yml` 的 `environment`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/lineage.db` | SQLite 数据库文件路径，目录不存在会自动创建 |
| `PORT` | `8000` | 容器内监听端口（compose 已映射 `8000:8000`；如改端口需同步修改端口映射） |
| `DECIMAL_SCALE` | `9` | 体积与各来源份额保留的小数位数；请求体积超出该精度会被拒绝（422） |
| `PROPORTION_SCALE` | `6` | 查询结果中来源占比（proportion）的小数位数 |
| `DECIMAL_PRECISION` | `50` | Decimal 中间运算（除法等）的有效数字精度，最小 28 |

---

## 数据模型

- **初始样品**：登记一个带初始体积的容器，它是自身唯一的祖先来源。
- **空容器**：预先登记的空容器，可作为转移目标。
- 每个容器的状态记录为若干条 `{祖先初始样品 ID -> 绝对体积份额}`，份额之和即当前体积。
  Decimal 在数据库中以**字符串**存储。
- 每个容器另持有持久化 **`version`**（整数，创建为 0）：每被一批**成功提交**的转移
  触碰一次就 +1（同一批中多次出现也只 +1），用于乐观并发控制。
  已有数据库在启动初始化时会自动补列（已有行版本为 0），无需手工迁移。

---

## API 一览

> 请求 / 响应中的体积字段接受 JSON 数字或字符串，**推荐使用字符串**（如 `"40.50"`），
> 以避免 JSON 数字经 float 解析；响应统一输出为字符串。

### 1. 登记初始样品

```bash
curl -X POST http://localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{"sample_id":"S1","volume":"100"}'
# 201 -> {"sample_id":"S1","volume":"100.000000000"}
```

### 2. 登记空容器

```bash
curl -X POST http://localhost:8000/containers \
  -H 'Content-Type: application/json' \
  -d '{"container_id":"V1"}'
# 201 -> {"container_id":"V1"}
```

重复登记返回 `409 container_exists`。

### 3. 批量转移（整批原子，按数组顺序依次执行）

```bash
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[
    {"source":"S1","destination":"V1","volume":"40"},
    {"source":"S2","destination":"V1","volume":"60"}
  ]}'
# 200 -> {"applied":2,"message":"已依次提交 2 条转移","versions":{"S1":1,"S2":1,"V1":1}}
```

转移时从源容器按其**当时**各祖先来源的混合比例取出，累加到目标容器的同名来源上，
源容器对应份额同步扣减。成功提交后，每个被触碰容器的 `version` 恰好 +1，
并随响应的 `versions` 字段返回。

**乐观并发控制（可选）**：请求可携带 `expected_versions`（容器 ID -> 期望版本，
取值来自此前 `GET /containers/{id}` 返回的 `version`）：

```bash
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"40"}],
       "expected_versions":{"S1":0,"V1":0}}'
```

- 提供时**必须恰好覆盖本批全部源和目标容器**：缺少或多余都返回
  `422 invalid_expected_versions`；
- 版本校验与状态读取在**同一写事务**内完成：任一容器版本不符返回
  `409 stale_container`，**整批不落库**；
- 不提供 `expected_versions` 时行为与之前完全一致（不校验版本，版本仍正常递增）。

**整批拒绝**的情形（HTTP 状态码与错误码）：

| 情形 | 状态码 | `error.code` |
| --- | --- | --- |
| 源 / 目标容器不存在 | 404 | `unknown_container` |
| `source` 与 `destination` 相同 | 422 | `self_transfer` |
| 转移体积大于源容器当前体积 | 422 | `insufficient_volume` |
| 体积为 0、为负、非有限数，或小数位超过 `DECIMAL_SCALE` | 422 | `invalid_volume` |
| `expected_versions` 未恰好覆盖本批全部源和目标 | 422 | `invalid_expected_versions` |
| 任一容器当前版本与 `expected_versions` 不符 | 409 | `stale_container` |

错误响应形如：

```json
{"error":{"code":"insufficient_volume","message":"第 2 条转移源液不足：'V1' 当前 75.000000000，请求 9999.000000000，整批拒绝"}}
```

### 4. 预演批量转移（不落库）

`POST /transfers/preview` 使用与批量提交**完全相同的请求结构**（含可选的
`expected_versions`），在数据库当前状态上按数组顺序整批模拟（链式转移使用本批
前序模拟结果），返回：

- `steps`：每一步的**条目序号**与从各祖先来源**实际扣取的精确份额**（`taken` 之和
  精确等于该步请求体积，同样采用最大余额法）；
- `containers`：全部受影响容器（本批所有源 / 目标容器）在预演结束时的**最终体积与
  来源构成**，结构与 `GET /containers/{id}` 一致；其中 `version` 为**预测提交后的
  版本**（当前版本 +1），预演本身不递增、不写库。

预演与真实提交**共用同一套校验与分摊代码**（包括 `expected_versions` 的覆盖与版本
校验）：失败时返回的 HTTP 状态码、`error.code` 以及消息中的条目序号（“第 N 条转移”）
与 `/transfers/batch` 完全一致。预演只读不写，**成功或失败都不会改变数据库**，
可以安全地重复调用。

```bash
curl -X POST http://localhost:8000/transfers/preview \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[
    {"source":"S1","destination":"V1","volume":"40"},
    {"source":"S2","destination":"V1","volume":"60"},
    {"source":"V1","destination":"V2","volume":"25"}
  ]}'
```

```json
{
  "steps": [
    {"index": 1, "source": "S1", "destination": "V1", "volume": "40.000000000",
     "taken": [{"source_id": "S1", "amount": "40.000000000"}]},
    {"index": 2, "source": "S2", "destination": "V1", "volume": "60.000000000",
     "taken": [{"source_id": "S2", "amount": "60.000000000"}]},
    {"index": 3, "source": "V1", "destination": "V2", "volume": "25.000000000",
     "taken": [
       {"source_id": "S1", "amount": "10.000000000"},
       {"source_id": "S2", "amount": "15.000000000"}
     ]}
  ],
  "containers": [
    {"container_id": "S1", "version": 1, "volume": "60.000000000",
     "sources": [{"source_id": "S1", "amount": "60.000000000", "proportion": "1.000000"}],
     "proportion_scale": 6},
    {"container_id": "S2", "version": 1, "volume": "40.000000000",
     "sources": [{"source_id": "S2", "amount": "40.000000000", "proportion": "1.000000"}],
     "proportion_scale": 6},
    {"container_id": "V1", "version": 1, "volume": "75.000000000",
     "sources": [
       {"source_id": "S1", "amount": "30.000000000", "proportion": "0.400000"},
       {"source_id": "S2", "amount": "45.000000000", "proportion": "0.600000"}
     ],
     "proportion_scale": 6},
    {"container_id": "V2", "version": 1, "volume": "25.000000000",
     "sources": [
       {"source_id": "S1", "amount": "10.000000000", "proportion": "0.400000"},
       {"source_id": "S2", "amount": "15.000000000", "proportion": "0.600000"}
     ],
     "proportion_scale": 6}
  ]
}
```

上例中第 3 步从混合容器 `V1` 取 25：预演按前两步形成的 40/60 混合比分摊为
S1 取 10、S2 取 15；调用后数据库中 S1/S2 仍为 100、V1/V2 仍为空。

### 5. 查询容器当前体积、来源占比与版本

```bash
curl http://localhost:8000/containers/V1
```

```json
{
  "container_id": "V1",
  "version": 1,
  "volume": "75.000000000",
  "sources": [
    {"source_id": "S1", "amount": "30.000000000", "proportion": "0.400000"},
    {"source_id": "S2", "amount": "45.000000000", "proportion": "0.600000"}
  ],
  "proportion_scale": 6
}
```

- `version`：容器状态版本，创建为 0，每被一批成功提交的转移触碰一次 +1；
  可作为 `expected_versions` 的取值用于乐观并发控制。
- `amount`：该祖先来源在容器中的**绝对体积**（满足体积守恒，可跨容器累加核对）。
- `proportion`：占容器当前体积的比例，按 `PROPORTION_SCALE` 四舍五入，仅用于展示。
- 空容器返回 `"version":0`、`"volume":"0"`、`"sources":[]`；未知容器返回 `404 unknown_container`。

---

## 精确性说明

转移量在各祖先来源之间的分摊采用**最大余额法**（Hare quota + Largest remainder）：
每份先向下取整到 `10^-DECIMAL_SCALE`，再把剩余的整数个刻度按被舍掉的余额从大到小补回。
由此保证：

- 各来源取出额之和**精确等于**请求转移体积；
- 每一份额满足 `0 ≤ 取出额 ≤ 源容器中该来源的份额`，因此不会出现“取超了”或负值；
- 与真实比例的偏差不超过 1 个体积刻度；
- 整个系统体积守恒：所有容器的 `amount` 总和始终等于初始样品体积总和。

注意 `proportion` 是四舍五入后的展示值，各比例之和可能不严格等于 1；需要精确核对时请使用
`amount` 字段。

---

## 本地开发（不使用 Docker）

要求 Python 3.11+：

```bash
pip install -r requirements.txt

# 启动（默认在 ./lineage.db 建库；可用环境变量覆盖）
DATABASE_PATH=./lineage.db uvicorn app.main:app --reload --port 8000

# 运行测试
pip install -r requirements-dev.txt
pytest -q
```

## 项目结构

```
app/
  config.py     # 环境变量配置
  db.py         # SQLite 连接、建库（启动时执行）
  schemas.py    # Pydantic 请求/响应模型（Decimal <-> 字符串）
  service.py    # 核心逻辑：登记、批量转移事务、比例分摊、查询
  main.py       # FastAPI 路由与错误处理
tests/          # pytest 端到端与分摊算法测试
Dockerfile
docker-compose.yml
docker-entrypoint.sh
```
