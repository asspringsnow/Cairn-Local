# Dispatcher 设计（内置 agent 版）

---

## 本质

Dispatcher 是 Cairn 的执行控制面。它负责：

1. 读取项目图状态，决定当前该跑 `bootstrap`、`reason` 还是 `explore`
2. 选择 worker、控制并发
3. 为每个任务启动一个内置 `cairn.agent.worker` 子进程
4. 维护 Intent 心跳、reason lease、超时、取消与 conclude 收尾
5. 代 agent 写回 Fact / Intent，是唯一的协议写入者

Agent 不直接认领 Intent、不直接 heartbeat、不直接调用 Cairn API。Agent 只接收 prompt，在项目工作区里调用工具，最后返回结构化 JSON。

---

## 和旧版的关系

Cairn 的图协议、任务模型、调度语义保持不变。变化只在执行内核：

| 层 | 旧版 | 当前 |
|---|---|---|
| 图 / 任务 / 调度 | Fact/Intent/Hint、bootstrap/reason/explore、并发调度 | 不变 |
| 执行内核 | 调 `pi` / `claude` / `codex` CLI | `cairn.agent.worker` 子进程 |
| 模型配置 | `dispatch.yaml` 的 worker env | Web UI / SQLite Settings |
| 运行环境 | Docker 容器或本机 CLI | 本机项目工作区 |

不再有容器模式，也没有外部 agent CLI 依赖。

---

## 架构

```
Cairn Server (FastAPI + SQLite)
  Projects / Facts / Intents / Hints / Settings
        ▲
        │ HTTP
        ▼
Dispatcher
  ├─ 调度循环（线程池，按任务并发）
  ├─ Intent / reason 心跳
  ├─ 项目工作区管理
  └─ 每个任务 spawn 一个 cairn.agent.worker
        │ JSON Lines over stdin/stdout
        ▼
cairn.agent.worker
  ├─ OpenAI 兼容 / Anthropic 客户端
  ├─ LLM 工具循环
  ├─ bash / read / write / edit / grep / find / ls
  ├─ 上下文预算与压缩
  └─ session JSON 落盘
```

### 为什么是子进程

模型请求和工具执行都可能阻塞很久。把它们放在独立进程里：

- 不占 Dispatcher 的线程/GIL
- 项目停止、任务超时、项目删除时可以直接杀进程组
- 一个任务一个进程，和旧版 kill `pi` 的语义一致
- 同一任务的 execute / conclude 复用一个 session 文件

---

## 任务类型

### Bootstrap

项目初始态（只有 `origin` / `goal`）直接尝试解决问题。

- 使用保留 Intent：`from=["origin"]`、`description="bootstrap"`、`creator="dispatcher.bootstrap"`
- 主阶段返回 `{fact, complete}`
- 主阶段超时 / 解析失败 → 同一 session 发 `bootstrap_conclude.md`，只收一个 Fact
- 先 `conclude` 写 Fact，再 `complete`，保证关键结果不绕开图

### Reason

读取整张图，判断：

1. 目标是否已满足 → `complete`
2. 是否需要提出新 Intent → `intents`
3. 当前不需要动作 → 空 data

通过项目级 `reason` lease 保证单项目最多一个 reason。

### Explore

认领一个 open Intent，执行探索，写回一个 Fact。

- 主阶段返回 `{description}`
- 主阶段超时 / 解析失败 → 同一 session 发 `explore_conclude.md`
- 失败时释放 claim，避免 Intent 被永久占住

---

## 调度

每 `runtime.interval` 秒一轮：

1. 回收任务与工作区清理结果
2. 从 Server 读取模型配置（`GET /settings/agent`）
3. 拉取项目列表与项目图
4. 非 active 项目：取消本地任务，按 `completed_action` 清理工作区
5. 按并发上限派发任务

派发优先级：

```
初始项目？
  ├─ bootstrap_enabled 且 worker 支持 bootstrap → bootstrap
  └─ 否则 → reason(initial)
非初始项目：
  ├─ 无 reason lease 且图有变化 → reason
  ├─ 有未认领且未本地运行的 Intent → explore（取最新）
  ├─ reason 已被占用 → 跳过
  └─ 图无变化 → 跳过
```

并发控制：

- `runtime.max_workers`：全局任务上限
- `runtime.max_running_projects`：同时调度的项目数
- `runtime.max_project_workers`：单项目任务上限
- `workers[].max_running`：单 worker 并发上限
- 项目轮转使用 `project_cursor`，避免固定顺序饿死

失败冷却：

- worker 健康检查失败 → `unhealthy`，冷却后重试
- agent 明确拒绝任务 → `rejected`，按项目/任务/worker 冷却

---

## 内置 agent 进程

### 启动

Dispatcher 用当前 Python 解释器启动：

```
python -u -m cairn.agent.worker
```

通过 stdin 发送 init：

```json
{"op":"init","settings":{...},"workspace":"...","session_path":"...",
 "system_prompt":"...","phase":"bootstrap"}
```

Worker 回 `{"type":"ready"}`。

### 执行

```json
{"op":"run","prompt":"...","tools":true,"timeout":600}
```

Worker 输出事件：

- `step`
- `assistant`
- `tool_call`
- `tool_result`
- `usage`
- `context_compaction`
- `step_limit`
- `result`（最终文本）
- `error`（含 kind/timeout/model）

Dispatcher 只把 `result` 的文本当作任务输出，交给 `contracts.py` 解析。

### 取消 / 超时

- Dispatcher 超时 → 杀 worker 进程组
- 项目 stop/completed/deleted → `TaskCancellation` 杀进程组
- 心跳丢失 → `HeartbeatLease` 杀进程组
- session 在每一步后落盘，因此 conclude 可以重新拉起进程继续

---

## 模型配置

模型配置存在 Server 的 SQLite `settings` 表：

- `provider`：`openai` / `anthropic`
- `base_url`
- `model`
- `api_key`
- `context_window`
- `max_tokens`
- `temperature`
- `request_timeout`

接口：

- `GET /settings`：UI 使用，API Key 只返回掩码与 `api_key_configured`
- `GET /settings/agent`：Dispatcher 使用，返回完整配置
- `PUT /settings`：保存；`agent.api_key` 为空表示保留旧 Key，`clear_api_key=true` 表示清除
- `POST /settings/test`：用当前或表单里的配置发一次最小模型请求

Dispatcher 每轮读取配置，所以 UI 修改后下一轮任务生效，不需要重启。

---

## 工具

| 工具 | 说明 |
|---|---|
| `bash` | 在项目工作区执行命令；合并 stdout/stderr；输出截断到 2000 行 / 50KB，完整输出落 `.cairn/outputs/` |
| `read` | 读文本文件，支持 offset/limit，截断时提示下一个 offset |
| `write` | 创建/覆盖文件，自动建父目录 |
| `edit` | 一次多个 exact replacement，要求唯一且不重叠，保留 BOM/换行 |
| `grep` | ripgrep 语义；无 rg 时回退 Python |
| `find` | 按 glob 找文件；无 rg 时回退 Python |
| `ls` | 列目录，目录带 `/` |

工具直接在宿主机执行，没有容器沙箱。

---

## 上下文管理

- 工具输出先截断，完整内容落文件
- 估算 token 超过 `context_window * 0.85` 时触发压缩
- 保留 system、首个 user、最近若干轮；更早的轮次用同一模型生成摘要
- 压缩按“轮”切分，避免把 assistant tool_calls 和 tool results 拆开
- 模型返回 context overflow 时，用 notice-only 压缩后重试一次

---

## 工作区

```
runtime.workspace_root/
└── <project_id>/
    ├── .cairn/sessions/<task>-<id>.json
    ├── .cairn/outputs/
    └── .cairn/prompts/
```

- `completed_action=keep`：项目完成后保留工作区
- `completed_action=remove`：项目完成后删除工作区
- 项目 stopped 时保留工作区，方便人工检查

---

## 配置

```yaml
server: "http://127.0.0.1:8000"
runtime:
  interval: 3
  max_workers: 8
  max_running_projects: 5
  max_project_workers: 4
  healthcheck_timeout: 15
  worker_healthcheck: "startup_only"   # startup_only | startup_and_task | disabled
  prompt_group: "default"
  workspace_root: "./workspaces"
  completed_action: "keep"             # keep | remove
tasks:
  bootstrap: {timeout: 120, conclude_timeout: 30}
  reason: {timeout: 300, max_intents: 3}
  explore: {timeout: 600, conclude_timeout: 120}
# workers 可省略，默认一个 native worker
```

模型/Key 不写进 `dispatch.yaml`。

---

## 已知限制

- 只支持单 Dispatcher 实例；多 Dispatcher 会重复 claim/调度
- 只有一份全局模型配置；不支持按 worker 配不同模型
- 没有容器沙箱；agent 以当前用户权限运行
- 工具没有交互式终端，长任务靠 timeout 控制
- Intent 只记录当前 claim 者，不保留 worker 历史

---

## 目录

```
cairn/src/cairn/
├── agent/                 # 内置 agent：客户端、工具、循环、session、worker
├── dispatcher/
│   ├── scheduler/         # 调度循环
│   ├── tasks/             # bootstrap/reason/explore + WorkerSession
│   ├── runtime/           # AgentProcess / WorkspaceManager / heartbeat / cancellation
│   ├── protocol/          # CairnClient
│   ├── workers/           # mock 测试驱动
│   └── prompts/           # 任务 prompt
└── server/                # FastAPI + SQLite + UI
```
