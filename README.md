# 号池控制台 · Endpoint Pool

把多个 OpenAI 兼容接口（各家代理站、中转站、自建网关都行）收进一个池子：
**对外只暴露一个地址，内部自动轮询，谁余额不足 / 报错 / 失联就自动切下一个，冷却到期自动复活，前端随时加号。**

- 一个 `/v1/...` 入口，客户端只认一个 base_url
- 轮询 / 粘性 / 加权 / 最少使用 / 优先级 / 随机 六种选号策略
- 粘性路由：同一会话、同一请求前缀永远落在同一个接口，才吃得满上游 prompt 缓存
- 402 / 余额不足 → **直接停用该号**（不是冷却），前端红底白字 + 31 天倒计时，到期自动回池；401/403、429、5xx、超时 → 换号 + 冷却
- 被停用的号退出路由，也不再被自动探活；人工停用的永远不会自动启用
- 冷却中的号会被剔除；自动探活通过后自动放回池子
- 全局默认 API Key：所有接口共用同一个 key 时只填一次，加号时密钥留空即可
- 可视化面板：可用 / 停用两区分区显示、加改删、启停、单号探活、批量导入、实时路由日志、策略与冷却参数热改
- 明 / 暗两套皮肤，右上角随时切换，记住选择（首次跟随系统）
- 密钥 Fernet 加密落库，前端只显示掩码
- Docker 一键跑，GitHub Actions 自动多架构构建推镜像
- 无前端构建步骤（原生 JS），镜像里只有 Python

---

## 目录

```
backend/
  app.py       FastAPI：管理 API + 代理路由 + 静态托管
  pool.py      选号策略 / 故障分类 / 冷却状态机 / 探活 / 日志广播
  proxy.py     上游转发：换号重试、流式透传、模型覆盖
  db.py        SQLite 持久层（endpoints / endpoint_state / settings / logs）
  crypto.py    密钥加密（Fernet）
  config.py    环境变量
frontend/
  index.html app.js style.css    原生控制台，无打包
tests/
  mock_upstream.py   假上游（按 key 返回 402/401/429/500/200）
  run_smoke.py       端到端冒烟：验证换号、冷却、探活、日志
Dockerfile  docker-compose.yml  .github/workflows/docker.yml
```

---

## 快速开始

### 1. Docker 直接跑

```bash
docker run -d --name endpoint-pool \
  -p 8000:8000 \
  -e ADMIN_TOKEN=your-panel-password \
  -e SECRET_KEY=$(openssl rand -hex 32) \
  -v $(pwd)/data:/data \
  --restart unless-stopped \
  ghcr.io/<你的GitHub用户名>/<仓库名>:latest
```

打开 `http://服务器IP:8000`，输入 `ADMIN_TOKEN` 进面板 → 右上角「+ 添加接口」。

### 2. docker compose

```bash
cp .env.example .env      # 改 ADMIN_TOKEN / SECRET_KEY
# 把 docker-compose.yml 里的 image 换成自己的仓库，或改成 build: .
docker compose up -d --build
```

### 3. 源码本地跑

```bash
pip install -r requirements.txt
export ADMIN_TOKEN=dev-token      # Windows: $env:ADMIN_TOKEN="dev-token"
uvicorn backend.app:app --reload --port 8000
python tests/run_smoke.py         # 可选：跑一遍换号逻辑验证
```

---

## 自动构建镜像（GitHub Actions）

`.github/workflows/docker.yml` 已经写好，推到 GitHub 就生效：

| 触发 | 行为 |
|---|---|
| push 到 `main` / `master` | 跑冒烟测试 → 构建 `linux/amd64` + `linux/arm64` → 推 `ghcr.io/owner/repo:latest` 和 `:main` |
| 打 tag `v1.2.3` | 额外推 `:1.2.3`、`:1.2` |
| PR | 只构建不推送（验证能编过） |
| 手动 | Actions 页面 `Run workflow` |

- 用内置 `GITHUB_TOKEN` 推 GHCR，**不需要额外配 secret**；包默认私有，第一次用完在仓库 Packages 里把可见性改成 public，或在部署机上 `docker login ghcr.io`。
- 想同时推 Docker Hub：仓库 Settings → Secrets 加 `DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`，工作流会多跑一步推 `docker.io/<用户名>/endpoint-pool`，没配自动跳过。
- 两个已踩过的坑（`python tests/check_workflow.py` 会拦）：
  - `if:` 里不能写 `secrets.*` —— if 条件不提供 secrets 上下文，GitHub 直接报 `Unrecognized named-value: 'secrets'`，包不包 `${{ }}` 都一样。正确做法是先落到 `env`，再判断 `env.DOCKERHUB_USER != ''`。
  - `metadata-action` 的 `images:` 里别放 `&&`/`||` 条件——secret 没配时会拼出空行，报 invalid reference。改成单独一步推送 Docker Hub。
- 改了代码直接 push，GitHub 那边编完，服务器上 `docker compose pull && docker compose up -d` 就是新版；想全自动可以在服务器加个 watchtower 或 webhook。

---

## 接口池怎么用

### 加号

面板「+ 添加接口」，或「批量导入」一次贴多行：

```
名称,BaseURL,APIKey
接口A,https://api-a.example.com,sk-xxx
接口B,https://api-b.example.com/v1,sk-yyy
https://api-c.example.com
```

第三行只给地址也行——**密钥留空就走「设置 → 全局默认 API Key」**。

字段说明：

| 字段 | 说明 |
|---|---|
| Base URL | 上游根地址，`https://api-a.example.com` 或 `https://api-a.example.com/v1` 都行，不会拼出 `/v1/v1` |
| API Key | 留空 = 用全局默认密钥；填了就加密入库，只显示掩码，编辑时留空 = 不修改 |
| 鉴权头名 / 前缀 | 默认 `Authorization` + `Bearer `，遇到自定义鉴权的站改成 `x-api-key` / 空前缀 |
| 强制模型 | 填了就覆盖请求体里的 `model`（各站模型名不一致时好用）；留空则透传 |
| 权重 | 「加权轮询」策略下的命中比例 |
| 优先级 | 「优先级」策略下只在高优先级组里轮询 |

### 全局默认 API Key

手上这把 key 在所有接口上通用时，别一个个填：

1. 设置 → 全局默认 API Key → 粘贴 key → 保存（加密存库，界面只回掩码）
2. 之后添加接口，API Key 那栏留空即可，表格里该行密钥处显示「默认密钥」
3. 改默认 key，所有用默认的接口一起生效；个别站要单独 key 的，就在该接口里单独填，接口自己的 key 优先

「默认鉴权头名 / 前缀」同样作用于用默认 key 的接口。要清掉默认 key，勾选「清空已保存的默认密钥」再保存。

### 客户端怎么连

```python
from openai import OpenAI

client = OpenAI(base_url="http://你的服务器:8000/v1", api_key="随便填，转发时不校验")
print(client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

- `base_url` 指到号池的 `/v1`，其余代码不用改。
- 想拿缓存命中，用 `sticky` 策略，并带上会话标识：
  ```python
  client = OpenAI(
      base_url="http://你的服务器:8000/v1",
      api_key="随便填",
      default_headers={"x-session-id": "我的会话-001"},
  )
  ```
  没传这个头也行——会用「模型 + system + 首条用户消息」算前缀哈希兜底。
- 响应头会带 `x-pool-endpoint`（实际命中的接口名，URL 编码）和 `x-pool-attempt`（第几次尝试成功），方便排查。
- 流式（`stream=true`）同样支持：先拿响应头判断成败，成功才开始往客户端吐字节，失败直接换号重来，不会吐半截。
- 非 OpenAI 路径也能代理：`http://服务器:8000/proxy/<任意路径>` 会原样转发到上游同路径。

### 故障判定与冷却

| 上游情况 | 判定 | 处理 |
|---|---|---|
| 402 Payment Required | 余额不足 | **自动停用该接口**（`enabled=0`），退出路由，前端红底白字「欠费停用」 |
| 401 / 403 | 鉴权失效 | 换号，冷却 `cooldown_auth`（默认 1800s） |
| 429 | 限流（响应体命中关键词则算余额不足 → 停用） | 换号，冷却 `cooldown_rate`（默认 30s） |
| 5xx / 408 / 425 | 上游故障 | 换号，冷却 `cooldown_server`（默认 60s） |
| 连接失败 / 超时 | 网络故障 | 换号，冷却 `cooldown_server` |
| 其它 4xx | 调用方参数错（不是号的错） | 原样透传给客户端，**不换号、不冷却** |

响应体里出现关键词（默认含 `insufficient_quota`、`insufficient balance`、`余额不足`、`额度不足`、`欠费`、`payment required` 等）时，即使状态码不是 402，也按余额不足处理——**同样直接停用**。关键词可在「设置」里改，逗号分隔。

`disable_on_quota`（默认开）控制这套行为，关掉就退回"冷却一段时间"。

**停用就是停用**：停掉的号退出路由、退出自动探活、退出批量探活，也不会因为"探活通过了"自己爬起来。启用只有三条路——点该行的「启用」、「重置」（重置 = 清冷却 + 启用 + 可选清统计），或者**欠费停用满期自动启用**（见下一条）。后台那个定时探活只服务「启用中且冷却中」的号（5xx / 限流那类），等它们恢复。

**欠费停用自动回池（默认 31 天）**：402 判定的停用会记下停用时间和到期时间，`quota_revive_days`（默认 31，0 = 永不自动启用）到点后由后台任务自动启用并清掉标记、重置状态。列表里这类号显示倒计时（`欠费停用 · 30天23小时后自动启用`），鼠标悬停能看到具体时刻。人工点「启用 / 重置」会立刻解除并取消计时，此后不再被自动启用打扰——**人工停用（不是 402 判的）永远不参与自动启用**。

单行「探活」按钮随时可以手动点，包括对停用的号——它只回报结果，不改你的开关。

列表按 **可用账号 / 停用账号** 两个分区显示，各带数量；停用区按"最快自动启用"排序，人工停用的排在最后。状态配色：绿 = 可用，黄 = 冷却中（带倒计时），红底白字 = 已停用，深红 = 欠费停用（带自动启用倒计时）。

一次请求最多换 `max_attempts` 个号（默认 5），全挂返回 `503` 并附上每个号的失败原因。冷却到期自动回池；「设置 → 自动探活间隔」不为 0 时，后台只探「启用中且冷却中」的号，通了立刻回池。

### 缓存命中：为什么轮询会掉命中率

上游的 prompt 缓存（KV cache）三家的算法不同，但键都是同一个东西：**账号 + 模型 + 前缀字节完全一致**。

| 上游 | 规则 |
|---|---|
| OpenAI | 自动生效，前缀 ≥1024 token，按 128 token 步进对齐，完全匹配才命中，闲置约 5–10 分钟过期，按组织/项目隔离 |
| Anthropic | 显式 `cache_control` 断点，默认 5 分钟（可开 1h），按 API key + 模型 + 前缀 |
| DeepSeek | 自动，按 64 token 块，按账号隔离 |

所以 **`round_robin` 是缓存杀手**：一个对话的第 1、2、3 轮被摊到 3 个不同的号上，每个号都只看到一段从没见过的前缀，命中率 ≈ 1/N；如果这些接口背后本来就是不同的上游账号，命中率直接是 0，因为它们根本不在同一个缓存域里。命中与不命中差的不只是钱（OpenAI 缓存输入约为原价的 0.1–0.25 倍），还有首 token 延迟。

**`sticky` 策略**就是为这件事做的：

1. 有 `x-session-id` 头（头名可改）就用它当会话键；
2. 没有就退回 `user` 字段、`metadata.user_id`，再没有就用 **`model` + system/developer 段 + 第一条用户消息** 做哈希——对话往下长，这个哈希不变，正好对应上游「前缀匹配」的语义；
3. 会话表 `affinity` 里存 `会话 → 接口`，`sticky_ttl`（默认 1800s）内固定命中同一个号；
4. 那个号冷却或失败时，自动换到一致性哈希的下一家，并把会话表改写过去，后续轮次继续跟新号走；
5. 增删接口时一致性哈希只搬 1/N 的会话，不会全体漂移。

**粒度是「会话」，不是「全局」**：不同会话各自绑各自的号，几十个对话同时跑就分布在几十个号上，不会把配额全压在一个号上。`affinity` 表存的是「这个会话用哪个号」的备忘录，不是全局单活开关。

真要「全局只用一个号、挂了才换下一个」（主备模式），用 `priority` 策略：主号优先级 10、备号 5、再备 1，池子只在最高优先级那一组里挑，整组都冷却了才降级。

`sticky_spread` 决定**新会话**怎么挑号：`hash`（默认）= 一致性哈希，同一个键永远同一家，稳定但不保证均摊；`least_used` = 按当前请求量挑最闲的号，均摊更好。已经绑定过的会话在两种模式下都不受影响，不会来回跳。

客户端配合这几条，命中率才真正上去：

- system prompt 里别塞时间戳、UUID、随机排序的 few-shot——前缀一个字节变了就全灭；
- `messages` 只追加、不重排、不改写历史；
- `tools` 定义顺序和内容保持稳定；
- 同一批批处理任务带同一个 `user` / `x-session-id`，让它们共用一段前缀；
- 长文档问答先把文档放前面、问题放后面。

什么时候别用 sticky：请求都很短、彼此没有共享前缀、或者你要的是严格均摊配额——那就 `round_robin`。想知道到底命没命中，看上游响应里的 `usage.prompt_tokens_details.cached_tokens`；本池额外在响应头给了 `x-pool-endpoint`，反复出现同一个名字说明粘住了。

### 面板上的操作

| 按钮 | 作用 |
|---|---|
| 探活 | 用「设置 → 探活路径」（默认 `/v1/models`）打一发。启用中的号通了即回池；停用的号只回报结果，不改开关 |
| 全部探活 | 对所有启用的号并发探一遍 |
| 启停 | 停用后立刻退出轮询，保留统计 |
| 重置 | 清掉冷却和最近错误；`counters=true` 连统计一起清 |
| 实时日志 | 每条路由决策：命中谁、状态码、耗时、为什么换号 |
| 立即清理 | 设置页按钮 / `POST /api/maintenance`：按保留策略清一次 + 截断 WAL + 处理到期的自动启用 |
| 停用账号区 | 欠费停用的号显示「还有多久自动启用」，到点后台自动放回可用区；人工停用的不会自己动 |
| 粘性路由 | 设置里配 `sticky_ttl`（会话保持秒数）和会话标识头名（默认 `x-session-id`） |
| 亮色 / 暗色 | 右上角切换皮肤，写进 localStorage；首次访问跟随系统 `prefers-color-scheme` |

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_TOKEN` | 空 | 面板口令。**留空 = 面板不设防**，只该在本机这么跑 |
| `PROXY_TOKEN` | 空 | 设了以后 `/v1/*` 要求请求头 `x-proxy-token`；留空则不校验（建议靠反代/内网限制来源） |
| `SECRET_KEY` | 自动生成 | 加密 api_key 的主口令。改了这个值，库里旧的密钥就解不开，必须重新录入 |
| `DATA_DIR` | `data` | SQLite 与密钥文件位置，容器里固定 `/data` |
| `LOG_KEEP` | `3000` | 日志保留条数的**初始默认值**，只在第一次建库时写进设置，之后在面板改 |
| `PORT` | `8000` | 监听端口 |

---

## 磁盘占用与自动清理

不会把盘塞爆，三层都在跑：

1. **后台每 5 分钟自动裁剪**（`housekeeping_loop`），双阈值，谁先到砍谁：
   - `log_keep`：日志最多留多少条（默认 3000，0 = 不限）
   - `log_keep_hours`：只留最近多少小时（默认 168 = 7 天，0 = 不限）
2. **会话粘性表也有上限**：`affinity_max`（默认 20000 行），超了按最久未用淘汰；另外 `sticky_ttl × 2` 之外的过期行也会被清。
3. **WAL 截断**：每次清理后跑 `PRAGMA wal_checkpoint(TRUNCATE)`。SQLite 的 `-wal` 文件不截断会一直涨，这是最容易忽略的一处。副产物：删行后文件不会自己缩，所以每天最多检查一次空页占比，超过 30% 且库够大才 `VACUUM`。

面板上能看见实时的「日志条数」和「库体积」两张卡，设置页有「立即清理一次」按钮（`POST /api/maintenance`，等价于手动跑一遍上面三步，并返回清理前后的数据）。

真实量级参考：一次请求写 1 条路由日志（约 150 字节），3000 条大约 0.5 MB；库稳定在 1 MB 上下，跟跑多久无关。

## 运维要点

- **备份**：全部状态在 `DATA_DIR/pool.db`（WAL 模式）。备份 `.db` 加上 `.secret`（或记住 `SECRET_KEY`）才能还原出密钥。
- **多副本**：SQLite 单写入，一个实例一份卷；要横向扩就先别多开，或把 `db.py` 换成 Postgres（接口是集中在一层里的，改起来不散）。
- **放在公网**：一定设 `ADMIN_TOKEN`；`/v1` 想公开给应用用就设 `PROXY_TOKEN` 或前置一层反代鉴权；`/api/pool/status` 是公开的轻量状态接口，不含任何密钥。
- **上游是第三方代理站**：密钥会以 `Authorization: Bearer <key>` 发往你填的 `base_url`，只加你信得过的地址。
- **环境代理**：容器里若配了 `HTTP(S)_PROXY`，转发会走它；但如果 `NO_PROXY` 写得不合法（例如 `[::1]` 这种带方括号的写法，httpx 解析会报 InvalidURL），启动后会在日志里看到「环境代理配置无法解析，改为直连上游」，然后自动退化为直连，不会因此全线失败。

---

## 自测

```bash
python tests/run_smoke.py        # 端到端：假上游 + TestClient
python tests/check_frontend.py   # 明暗主题变量 / 设置控件 / 文案无残留域名
python tests/check_workflow.py   # 工作流里没有 secrets-in-if、没有空 images
```

`run_smoke.py` 起三个假上游（分别返回 402 余额不足 / 401 鉴权失败 / 200 正常），验证：轮询命中、402 自动停用、停用号不路由/不批量探活/探活不自动启用、欠费停用满 31 天自动启用（把到期时间拨到过去验证）、人工停用不参与自动启用、坏号冷却、后续请求跳过冷却号、5xx 与 429 换号、探活恢复、`base_url` 带 `/v1` 不重复拼接、全局默认密钥生效与清空、粘性路由（同一会话固定同号、追加消息后前缀仍粘住）、设置校验、日志落库、静态前端可访问、删除。三个脚本都已挂进 CI 的 smoke job。
