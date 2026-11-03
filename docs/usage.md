# Cairn-Local 使用文档

本文档面向实际部署和使用 Cairn-Local 的操作者，覆盖安装、模型配置、任务流程、并发调优、日志排障和备份。

Cairn-Local 是 Cairn 的二开版本：保留 Fact / Intent / Hint 图、bootstrap / reason / explore 调度、Intent 认领、心跳、lease、超时取消和 conclude 收尾；把外部 `pi` / `claude` / `codex` CLI 和容器运行时替换为 **Cairn 内置 native agent**。

---

## 1. 环境要求

- Linux，推荐 Kali
- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)
- 一个可用的模型接口：
  - OpenAI 兼容：`/chat/completions`
  - Anthropic：`/v1/messages`
- 能访问模型 API 的网络

不需要：

- `pi` / `claude` / `codex` CLI
- Docker / docker-compose
- worker 容器镜像

---

## 2. 安装与启动

```bash
git clone https://github.com/guaidao2/Cairn-Local.git
cd Cairn-Local

# 1. 检查 Python / uv / curl 和模型配置状态
bash scripts/check-env.sh

# 2. 安装依赖；如果 dispatch.yaml 不存在则从模板生成
bash scripts/setup.sh

# 3. 一键启动 server + dispatcher
bash scripts/start.sh
```

默认地址：

```text
http://<机器 IP>:8000
```

常用命令：

```bash
bash scripts/start.sh status
bash scripts/start.sh logs dispatch
bash scripts/start.sh logs server
bash scripts/start.sh restart
bash scripts/start.sh stop
```

如果 8000 已被其他服务占用：

```bash
CAIRN_PORT=8010 bash scripts/start.sh
```

`start.sh` 启动前会把 `dispatch.yaml` 的 `server` 端口同步成当前 `CAIRN_PORT`，避免出现 server 在 8000、dispatcher 却连 8010 的错位。

> `scripts/setup.sh` 只在 `dispatch.yaml` 不存在时生成；已有配置会保留。修改端口后，优先用 `CAIRN_PORT=... bash scripts/start.sh`，不要手动只改其中一边。

---

## 3. 配置模型

打开 UI：

```text
服务器设置 -> Agent 模型
```

填写：

| 字段 | 说明 |
|---|---|
| 协议 | `OpenAI 兼容` 或 `Anthropic` |
| Base URL | 例如 `https://api.deepseek.com/v1` |
| 模型 | 例如 `deepseek-chat` / `deepseek-flash` |
| API Key | 保存后 UI 只显示掩码；留空表示不修改 |
| 上下文长度 | 模型上下文窗口，默认 `1,000,000` |
| 最大输出 | 单次请求输出上限，默认 `32768` |
| 温度 | 默认 `0.6` |
| 请求超时 | 单次模型 HTTP 请求超时，默认 `300` 秒 |

点 **测试连接** 后保存。Dispatcher 会在下一轮任务自动读取新配置，**不需要重启**。

注意事项：

- 模型配置保存在服务器 SQLite（`datas/cairn.db`），不写入 `dispatch.yaml`。
- `GET /settings/agent` 只允许 loopback 访问；dispatcher 的 `server` 必须是 `http://127.0.0.1:<port>` 或运行在同一台机器。
- `测试连接` 只发送一个很小的请求，**不能验证你设置的 `max_tokens` 是否被模型接受**。换模型后建议先设保守值，跑一个真实任务再调高。
- DeepSeek 的 reasoning 会消耗输出预算。`16384` 可能被 reasoning 吃满，导致最终 JSON 为空；当前默认 `32768`。如果模型服务端拒绝更大的值，请在 UI 调低。
- `max_tokens` 是请求上限，不是模型能力上限；模型 catalog 里的更大数字不等于请求一定能用。

---

## 4. 任务与图

每个项目有两个初始 Fact：

- `origin`：起点、目标地址、题目描述、附件等
- `goal`：要达成的目标

运行过程中：

- **Fact**：已确认的客观结果，只增不改
- **Intent**：一个探索方向，未完成时没有 `to`
- **Hint**：人工注入的方向、补充信息或纠偏

三个阶段：

| 阶段 | 作用 | 工具 |
|---|---|---|
| `bootstrap` | 直接尝试解决问题 | 完整工具 |
| `reason` | 看整张图，判断完成 / 提新 Intent / 空操作 | **无工具，只读 prompt 内联图** |
| `explore` | 认领一个 Intent，执行并写 Fact | 完整工具 |

reason 不读项目工作区，也不执行命令。它只根据 graph YAML 做图级决策；如果证据不足，应提出 Intent，让 explore 去取证。需要补充证据时，优先把证据写进 Fact / Hint，而不是让 reason 自己翻源码。

execute 和 conclude 复用同一个 session；主阶段超时或输出无法解析时，conclude 仍能基于已有工具结果收尾。

### 图片 read

`read` 除了文本文件，也支持图片：

- png / jpg / jpeg / gif / webp / bmp
- 按文件内容嗅探 MIME，不依赖扩展名（错误扩展名也能识别）
- 原始文件最大 20MB；自动缩放到最长边 2048，并压缩到 ≤3MB 的 provider 安全大小
- BMP 会自动转成 PNG/JPEG 后发送
- 图片会作为 image content block 附在 tool result 上，不是 base64 文本
- 需要模型本身支持 vision；纯文本模型可能返回 400，请换 vision 模型或不要读取图片
- `offset` / `limit` 只适用于文本文件
- 图片太大时仍可先用 `bash` 压缩/缩放，减少 token 和延迟

---

## 5. 并发与时限

`dispatch.example.yaml` 当前默认：

```yaml
runtime:
  interval: 3
  max_workers: 8
  max_running_projects: 5
  max_project_workers: 4
  healthcheck_timeout: 15

tasks:
  bootstrap:
    timeout: 120
    conclude_timeout: 30
  reason:
    timeout: 300
    max_intents: 3
  explore:
    timeout: 600
    conclude_timeout: 120

workers:
  - name: "cairn"
    type: "native"
    max_running: 8
```

三个并发上限不要混淆：

| 配置 | 含义 |
|---|---|
| `max_workers` | 全局最多同时运行多少个任务 |
| `max_running_projects` | 同时调度多少个项目 |
| `max_project_workers` | **单个项目**最多同时运行多少个任务 |
| `workers[].max_running` | native worker 自己最多接多少个任务 |

所以 **单项目只同时跑 4 个任务**，是 `max_project_workers: 4` 的结果，不是 worker 只开了 4 个。要提高单项目并发，需要同时保证：

```yaml
max_project_workers <= max_workers
max_project_workers <= workers[].max_running
```

并发越高，token 消耗、模型限流、长尾和上下文压力都会上升。比赛/CTF 建议从 4–6 开始，不要盲目拉满。

修改 `dispatch.yaml` 后：

```bash
bash scripts/start.sh restart
```

---

## 6. 工作区与 session

```text
workspaces/
└── proj_001/
    ├── .cairn/
    │   ├── sessions/     # 任务对话/session；execute 与 conclude 复用
    │   ├── outputs/      # bash 完整输出
    │   └── prompts/      # 图快照等调试文件
    └── ...               # agent 的 cwd，扫描结果、脚本、证据
```

- bash 输出只把尾部返回模型，完整内容落 `.cairn/outputs/`
- 需要细节时让 agent `read` 对应输出文件
- 项目完成后由 `runtime.completed_action` 决定 `keep` 还是 `remove` 工作目录
- 删除项目/停止项目前，确认工作区里没有还要保留的证据

---

## 7. 常见故障

### 7.1 dispatcher 日志一直 `Connection refused` 或 `/settings/agent` 404

先确认 server 和 dispatcher 的端口一致：

```bash
grep '^server:' dispatch.yaml
ss -lntp | grep -E ':(8000|8010) '
curl -s http://127.0.0.1:8000/settings
```

如果 server 在 8000、`dispatch.yaml` 写 8010，就会连错。用：

```bash
CAIRN_PORT=8000 bash scripts/start.sh restart
```

`start.sh` 会自动同步 `dispatch.yaml` 的 server 端口。

如果 `/settings/agent` 403，说明 dispatcher 使用了非 loopback 地址，或 server 不是本机；改成 `http://127.0.0.1:<port>`。

### 7.2 reason 一直 `no JSON object found` 或输出为空

看失败 session 的 usage：

```bash
# 在 workspaces/<project>/.cairn/sessions/ 下查看最新 JSON
# completion_tokens 是否正好等于 max_tokens
```

如果 `completion_tokens` 打满、assistant content 为空，说明 reasoning 吃完了输出预算。提高 UI 里的 **最大输出**（例如 32768/65536），保存后下一轮生效。

另外确认 dispatcher 的刷新签名已包含 `max_tokens`；本版本已包含，修改 UI 后不需要重启。

### 7.3 只同时跑 4 个任务

检查：

```yaml
runtime.max_project_workers
```

默认是 4；单项目上限受它控制。

### 7.4 页面打开是空白

不要直接双击 `index.html` 用 `file://` 打开。UI 依赖 HTTP API、Alpine/Tailwind 和 `/projects`、`/settings` 等接口；请通过：

```text
http://<机器 IP>:8000
```

访问。

### 7.5 模型 400 / 测试连接显示正常但任务失败

`测试连接` 只发小请求，不验证大 `max_tokens`。检查：

- 模型名是否正确
- `max_tokens` 是否超过该模型允许值
- Base URL 是否是完整的 `/v1` 或 `/v1/messages` 前缀
- API Key 是否在保存后仍有效

先调低 `max_tokens`，再跑真实任务。

### 7.6 项目没有反应

检查：

```bash
curl -s http://127.0.0.1:8000/projects
bash scripts/start.sh logs dispatch
```

确认：

- 项目 `status` 是 `active`
- dispatcher 日志没有 `skip dispatch because no active projects`
- 模型健康检查通过
- 没有 `Connection refused` / 404
- 单项目并发没有被占满

---

## 8. 安全注意

- 默认监听 `0.0.0.0`，**没有内置登录/鉴权**；任何能访问端口的人都能创建项目并让 agent 在宿主机执行命令。
- 仅用于授权内网、比赛虚拟机或你完全控制的网络；不要暴露到公网。
- API Key 存在服务器 SQLite，不会写进 `dispatch.yaml`，但数据库文件本身要按敏感文件保护。
- 不要提交 `dispatch.yaml`、`datas/`、`workspaces/`、`logs/`、`*.db`、`*.session.json`。
- 删除项目、停止项目、清理工作区前确认没有还要保留的证据。

---

## 9. 备份、升级与测试

需要备份的内容：

```text
datas/cairn.db       # 项目、Fact/Intent/Hint、模型配置
workspaces/          # 项目工作区、session、工具输出
dispatch.yaml        # 调度与并发配置
logs/                # 排障日志
```

升级：

```bash
cd Cairn-Local
git pull
bash scripts/setup.sh
bash scripts/start.sh restart
```

测试：

```bash
uv run --project cairn --group dev pytest -q
```

不接真实模型、只验证调度/协议/UI 流程：

```bash
uv run --project cairn cairn dispatch --config dispatch_mock.yaml
```

`dispatch_mock.yaml` 使用确定性 mock worker，不消耗 API 额度。

---

## 10. 许可与致谢

本项目保持 AGPLv3。原项目作者：leixiao / oritera；二开维护：guaidao2。详见 `COPYRIGHT` 和 `LICENSE`。
