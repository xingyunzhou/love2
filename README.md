# 号池控制台 · Endpoint Pool

把多个 OpenAI 兼容接口（比如你手上那批 `*.lovable.app` 代理站）收进一个池子：
**对外只暴露一个地址，内部自动轮询，谁余额不足 / 报错 / 失联就自动切下一个，冷却到期自动复活，前端随时加号。**

- 一个 `/v1/...` 入口，客户端只认一个 base_url
- 轮询 / 加权 / 最少使用 / 优先级 / 随机 五种选号策略
- 402、401/403、429、5xx、超时、响应体命中「余额不足」关键词 → 立刻换号 + 冷却
- 冷却中的号会被剔除；自动探活通过后自动放回池子
- 可视化面板：加、改、删、启停、单号探活、批量导入、实时路由日志、策略与冷却参数热改
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
接口A,https://aaa.lovable.app,sk-xxx
接口B,https://bbb.lovable.app,sk-yyy
https://ccc.lovable.app,sk-zzz
```

字段说明：

| 字段 | 说明 |
|---|---|
| Base URL | 上游根地址，`https://x.lovable.app` 或 `https://x.lovable.app/v1` 都行，不会拼出 `/v1/v1` |
| API Key | 加密入库；编辑时留空 = 不修改 |
| 鉴权头名 / 前缀 | 默认 `Authorization` + `Bearer `，遇到自定义鉴权的站改成 `x-api-key` / 空前缀 |
| 强制模型 | 填了就覆盖请求体里的 `model`（各站模型名不一致时好用）；留空则透传 |
| 权重 | 「加权轮询」策略下的命中比例 |
| 优先级 | 「优先级」策略下只在高优先级组里轮询 |

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
- 响应头会带 `x-pool-endpoint`（实际命中的接口名，URL 编码）和 `x-pool-attempt`（第几次尝试成功），方便排查。
- 流式（`stream=true`）同样支持：先拿响应头判断成败，成功才开始往客户端吐字节，失败直接换号重来，不会吐半截。
- 非 OpenAI 路径也能代理：`http://服务器:8000/proxy/<任意路径>` 会原样转发到上游同路径。

### 故障判定与冷却

| 上游情况 | 判定 | 处理 |
|---|---|---|
| 402 Payment Required | 余额不足 | 换号，冷却 `cooldown_quota`（默认 1800s） |
| 401 / 403 | 鉴权失效 | 换号，冷却 `cooldown_auth`（默认 1800s） |
| 429 | 限流（响应体命中关键词则按余额不足算） | 换号，冷却 `cooldown_rate`（默认 30s） |
| 5xx / 408 / 425 | 上游故障 | 换号，冷却 `cooldown_server`（默认 60s） |
| 连接失败 / 超时 | 网络故障 | 换号，冷却 `cooldown_server` |
| 其它 4xx | 调用方参数错（不是号的错） | 原样透传给客户端，**不换号、不冷却** |

响应体里出现关键词（默认含 `insufficient_quota`、`insufficient balance`、`余额不足`、`额度不足`、`欠费`、`payment required` 等）时，即使状态码是 200 之外的任何码，也按余额不足处理。关键词可在「设置」里改，逗号分隔。

一次请求最多换 `max_attempts` 个号（默认 5），全挂返回 `503` 并附上每个号的失败原因。冷却到期自动回池；「设置 → 自动探活间隔」不为 0 时，后台会周期性探活冷却中的号，通了立刻恢复。

### 面板上的操作

| 按钮 | 作用 |
|---|---|
| 探活 | 用「设置 → 探活路径」（默认 `/v1/models`）打一发，成功即恢复为可用 |
| 全部探活 | 对所有启用的号并发探一遍 |
| 启停 | 停用后立刻退出轮询，保留统计 |
| 重置 | 清掉冷却和最近错误；`counters=true` 连统计一起清 |
| 实时日志 | 每条路由决策：命中谁、状态码、耗时、为什么换号 |

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_TOKEN` | 空 | 面板口令。**留空 = 面板不设防**，只该在本机这么跑 |
| `PROXY_TOKEN` | 空 | 设了以后 `/v1/*` 要求请求头 `x-proxy-token`；留空则不校验（建议靠反代/内网限制来源） |
| `SECRET_KEY` | 自动生成 | 加密 api_key 的主口令。改了这个值，库里旧的密钥就解不开，必须重新录入 |
| `DATA_DIR` | `data` | SQLite 与密钥文件位置，容器里固定 `/data` |
| `LOG_KEEP` | `3000` | 日志保留条数 |
| `PORT` | `8000` | 监听端口 |

---

## 运维要点

- **备份**：全部状态在 `DATA_DIR/pool.db`（WAL 模式）。备份 `.db` 加上 `.secret`（或记住 `SECRET_KEY`）才能还原出密钥。
- **多副本**：SQLite 单写入，一个实例一份卷；要横向扩就先别多开，或把 `db.py` 换成 Postgres（接口是集中在一层里的，改起来不散）。
- **放在公网**：一定设 `ADMIN_TOKEN`；`/v1` 想公开给应用用就设 `PROXY_TOKEN` 或前置一层反代鉴权；`/api/pool/status` 是公开的轻量状态接口，不含任何密钥。
- **上游是第三方代理站**：密钥会以 `Authorization: Bearer <key>` 发往你填的 `base_url`，只加你信得过的地址。

---

## 自测

```bash
python tests/run_smoke.py
```

起三个假上游（分别返回 402 余额不足 / 401 鉴权失败 / 200 正常），验证：轮询命中、坏号冷却、后续请求跳过冷却号、5xx 与 429 换号、探活恢复、`base_url` 带 `/v1` 不重复拼接、设置校验、日志落库、静态前端可访问、删除。
