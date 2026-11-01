#!/usr/bin/env bash
# Cairn 环境检测。只检查，不安装、不修改配置。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${CAIRN_PORT:-8000}"
fail=0
warn=0

say() { printf '%s\n' "$*"; }
ok() { printf '  [+] %s\n' "$*"; }
miss() { printf '  [-] %s\n' "$*"; fail=$((fail + 1)); }
note() { printf '  [!] %s\n' "$*"; warn=$((warn + 1)); }

say "==============================================="
say " Cairn 环境检测"
say " 目录: $ROOT"
say "==============================================="

say "[系统]"
os="$(uname -s 2>/dev/null || echo unknown)"
case "$os" in
  Linux) ok "Linux ($(uname -r))" ;;
  *) note "当前系统是 $os；Cairn 的内置 agent 按 Linux 设计，建议在 Kali/Linux 上运行" ;;
esac
if [ -r /etc/os-release ]; then
  ok "$(grep -E '^PRETTY_NAME=' /etc/os-release | cut -d= -f2- | tr -d '"')"
fi

say "[基础命令]"
for cmd in bash curl git; do
  if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd -> $(command -v "$cmd")"; else miss "$cmd 未安装"; fi
done
if command -v rg >/dev/null 2>&1; then ok "rg -> $(command -v rg)"; else note "rg 未安装；agent 的 grep/find 会回退到 Python 实现"; fi
if command -v setsid >/dev/null 2>&1; then ok "setsid -> $(command -v setsid)"; else note "setsid 未安装；start.sh 会回退到普通后台启动"; fi

say "[Python / uv]"
if command -v python3 >/dev/null 2>&1; then
  pyver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo unknown)"
  case "$pyver" in
    3.1[2-9]|3.[2-9][0-9]) ok "python3 $pyver" ;;
    *) miss "python3 $pyver 低于 3.12" ;;
  esac
else
  miss "python3 未安装"
fi
if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version 2>/dev/null | head -1)"
else
  if [ -x "$HOME/.local/bin/uv" ]; then
    ok "uv -> $HOME/.local/bin/uv（当前 shell 未加入 PATH）"
  else
    miss "uv 未安装：https://docs.astral.sh/uv/"
  fi
fi

say "[项目配置]"
if [ -f "$ROOT/dispatch.yaml" ]; then
  ok "dispatch.yaml"
  if grep -q 'server:' "$ROOT/dispatch.yaml"; then
    ok "$(grep -m1 'server:' "$ROOT/dispatch.yaml")"
  fi
else
  note "缺少 dispatch.yaml；运行 bash scripts/setup.sh 生成"
fi
if [ -d "$ROOT/cairn/src/cairn" ]; then
  ok "源码目录 cairn/src/cairn"
else
  miss "源码目录不完整"
fi

say "[模型配置]"
if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/settings" >/dev/null 2>&1; then
  ok "server 可达 http://127.0.0.1:${PORT}"
  settings="$(curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/settings" 2>/dev/null || true)"
  if printf '%s' "$settings" | grep -q '"api_key_configured":true'; then
    ok "Agent 模型已配置"
  else
    note "Agent 模型未配置；打开 http://127.0.0.1:${PORT} -> 服务器设置 -> Agent 模型"
  fi
else
  note "server 未运行；启动后可在 Web UI 配置模型"
fi

say "[渗透常用工具（可选）]"
for cmd in nmap nuclei ffuf sqlmap tmux gobuster feroxbuster; do
  if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd"; else note "$cmd 未安装（不影响 Cairn 启动）"; fi
done

say "==============================================="
if [ "$fail" -gt 0 ]; then
  say "结果：$fail 项必须修复，$warn 项提醒"
  exit 1
fi
say "结果：关键环境正常，$warn 项提醒"
