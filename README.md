<div align="center">

<img src="./README/banner.png" alt="Cairn Banner"/>

# Cairn

### 从 AI 渗透测试出发的通用状态空间搜索

</div>

Cairn 是一个面向“已知起点、已知目标、未知路径”问题的探索引擎。渗透测试、CTF、漏洞研究、甚至数学证明，都可以看成同一个形状：从已知事实出发，不断探索，直到某些事实共同满足目标。

Cairn 用 **Fact / Intent / Hint** 构成的图来记录整个探索过程：

| 概念 | 含义 |
|------|------|
| **Fact** | 已确认的客观事实，写入图中，只增不改 |
| **Intent** | 一个已声明的探索方向，尚未得出结论 |
| **Hint** | 人随时注入的判断、方向或补充信息 |

图从 `origin` 生长到 `goal`。每个 Fact 是一块垫脚石，每个 Intent 是向未知迈出的一步。多个 agent 并发读写同一张图，通过共享环境间接协作（stigmergy），不需要互相直接通信。

## 这一版的变化

Cairn 不再依赖任何外部 agent CLI，也不再需要 Docker：

- **内置 agent**：Cairn 自己实现 LLM 工具循环（OpenAI 兼容 / Anthropic `/v1/messages`）
- **独立进程**：每个任务启动一个 `cairn.agent.worker` 子进程，不占用 dispatcher 的线程/GIL，超时和项目停止时直接杀进程组
- **模型配置在 UI**：Base URL、模型名、API Key、上下文长度、温度都可直接在 Web UI 配置，改完下一轮任务自动生效
- **不再需要** `pi` / `claude` / `codex`
- **不再有** worker 容器、docker-compose、容器运行时代码
- **保留 Cairn 的全部核心机制**：Fact/Intent/Hint 图、bootstrap/reason/explore、多任务并发、Intent 认领、心跳、reason lease、超时取消、conclude 收尾、图导出与 UI

内置 agent 的工具集：

- `bash`：在项目工作目录执行命令；大输出落文件，只把尾部返回模型
- `read` / `write` / `edit`：读写和精确替换文件
- `grep` / `find` / `ls`：搜索文件内容、按 glob 找文件、列目录

上下文接近窗口上限时会自动压缩：保留原始任务和最近的工作，把更早的轮次总结成摘要；完整命令输出始终保存在项目工作区的 `.cairn/outputs/` 下，可随时回读。

### 上下文长度与输出上限

默认 `context_window = 1,000,000`（pi 的 DeepSeek catalog 数据），`max_tokens = 16384`（pi 的 `model-config.ts` / `provider-composer.ts` 默认最大输出）。DeepSeek catalog 里的 `maxTokens: 384000` 是模型能力上限，不是 pi 的默认请求值；直接拿它当默认可能被服务端以 400 拒绝。请按你实际使用的模型填写这两个值。

内置 agent **不设置硬性的最大步数**，和 pi 的行为一致。任务由 dispatcher 的 phase timeout、项目停止、以及人工 Hint 控制；如果模型卡住，停止项目或注入 Hint 即可，不会由 agent 自己截断轮次。

## 架构

```
          ┌──────────────────────────────────┐
          │           Cairn Server           │
          │    Facts + Intents + Hints       │
          │    Settings（含模型配置）         │
          └─────────────────┬────────────────┘
                            │
                     Read / Write API
                            │
          ┌─────────────────┴────────────────┐
          │             Dispatcher           │
          │   调度任务 / 心跳 / 超时 / 写回    │
          └─────────────────┬────────────────┘
                            │ 每个任务一个子进程
          ┌─────────────────┴────────────────┐
          │        cairn.agent.worker        │
          │  LLM 循环 + bash/read/write/edit │
          │  上下文压缩 + session 落盘        │
          └──────────────────────────────────┘
```

每个项目在 `runtime.workspace_root` 下有一个独立工作目录，agent 的 cwd、session、工具输出都在里面。Dispatcher 仍然是唯一协议写入者，agent 不直接调用 Cairn API。

## 快速开始（Linux / Kali）

要求：

- Linux（推荐 Kali）
- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)
- 一个可用的 OpenAI 兼容或 Anthropic 模型接口

```bash
git clone https://github.com/guaidao2/Cairn-Local.git
cd Cairn

# 1. 环境检测
bash scripts/check-env.sh

# 2. 安装依赖 + 生成 dispatch.yaml
bash scripts/setup.sh

# 3. 一键启动 server + dispatcher
bash scripts/start.sh
```

启动后打开：

```
http://<你的机器 IP>:8000
```

> **局域网暴露说明**：`scripts/start.sh` 默认监听 `0.0.0.0`，方便比赛时 Kali 虚拟机开服务、物理机浏览器直接访问。Cairn 目前**没有内置登录/鉴权**，任何能访问该端口的人都能创建项目并让 agent 在宿主机执行命令。请只在授权内网、比赛虚拟机或你完全控制的网络中这样使用；不要把端口暴露到公网。

进入 **服务器设置 → Agent 模型**：

- 协议：OpenAI 兼容或 Anthropic
- Base URL：例如 `https://api.deepseek.com/v1`
- 模型名：例如 `deepseek-chat`
- API Key：粘贴你的 Key（保存后 UI 只显示掩码，留空表示不修改）
- 上下文长度、最大输出、温度、请求超时按需调整

点 **测试连接** 确认可用，再点 **保存服务器设置**。Dispatcher 下一轮任务会自动使用新配置，不需要重启。

其他命令：

```bash
bash scripts/start.sh status
bash scripts/start.sh logs dispatch
bash scripts/start.sh stop
bash scripts/start.sh restart
```

## 手动启动

```bash
# 终端 1：server
uv run --project cairn cairn serve --host 0.0.0.0 --port 8000

# 终端 2：dispatcher
uv run --project cairn cairn dispatch --config dispatch.yaml
```

只做模型/配置检查：

```bash
uv run --project cairn cairn dispatch --config dispatch.yaml --startup-healthcheck-only
```

## 配置

`dispatch.yaml` 由 `scripts/setup.sh` 从 `dispatch.example.yaml` 生成。关键项：

| 配置 | 说明 |
|------|------|
| `server` | Cairn Server 地址 |
| `runtime.interval` | 调度轮询与心跳周期（秒） |
| `runtime.max_workers` | 全局并发任务上限 |
| `runtime.max_running_projects` | 同时调度的项目数 |
| `runtime.max_project_workers` | 单项目并发上限 |
| `runtime.workspace_root` | 每个项目的工作目录根 |
| `runtime.completed_action` | 项目完成后 `keep` 或 `remove` 工作目录 |
| `tasks.bootstrap/reason/explore` | 各阶段超时、conclude 超时、max_intents |
| `workers` | 可省略；默认一个 `native` worker，模型配置来自 UI |

模型、Base URL、API Key **不写在 `dispatch.yaml` 里**，避免配置文件和 shell history 泄露密钥。

## 工作区

```
workspaces/
└── proj_001/
    ├── .cairn/
    │   ├── sessions/     # 每个任务的对话/session（结论阶段复用）
    │   ├── outputs/      # bash 完整输出
    │   └── prompts/      # 图快照文件
    └── ...               # agent 的 cwd，扫描结果、脚本、证据放这里
```

同一个任务的 execute 与 conclude 共用一个 session 文件。主阶段超时或输出无法解析时，conclude 会用同一 session 收尾，不会丢掉已经确认的工具结果。

## 测试

```bash
uv run --project cairn --group dev pytest
```

不接真实模型、只验证调度与协议：

```bash
uv run --project cairn cairn serve
uv run --project cairn cairn dispatch --config dispatch_mock.yaml
```

## 安全说明

内置 agent 的工具直接在宿主机上以当前用户身份执行，没有容器沙箱。请只在你有明确授权的环境中使用，并自行承担使用责任。不要对未授权的系统、网络或数据运行 Cairn。

## 进一步阅读

- [Dispatcher 设计](docs/specs/dispatcher-design.md)
- [Server 协议](docs/specs/server-protocol.md)

## 致谢

Cairn 原始项目由 **oritera / leixiao** 创建并维护（[oritera/Cairn](https://github.com/oritera/Cairn)），最初的 Fact/Intent/Hint 图协议、Dispatcher 调度和比赛验证都来自原项目。本仓库是 **guaidao2** 基于原项目维护的二开版本，感谢原作者和 Tencent Hackathon 团队的工作。

## License

Copyright (C) 2026 **guaidao2**（二开版本）；原始项目版权归 oritera / leixiao 所有。

本项目使用 **GNU AGPLv3**（许可证全文见 [LICENSE](LICENSE)，未修改原协议文本）。商业使用或希望在不承担 AGPL 开源义务的闭源环境中使用，请联系项目作者获取商业许可。

通过提交 Pull Request，即表示你同意你的贡献可以同时按 AGPL-3.0 和项目的商业许可使用。
