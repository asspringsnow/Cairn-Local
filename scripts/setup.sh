#!/usr/bin/env bash
# 安装依赖并生成一份可用的 dispatch.yaml。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${CAIRN_PORT:-8000}"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "[!] 找不到 uv。先安装：https://docs.astral.sh/uv/"
  exit 1
fi

echo "[*] 安装 Python 依赖"
uv sync --project "$ROOT/cairn" --group dev

if [ ! -f "$ROOT/dispatch.yaml" ]; then
  echo "[*] 生成 dispatch.yaml"
  cp "$ROOT/dispatch.example.yaml" "$ROOT/dispatch.yaml"
  if command -v sed >/dev/null 2>&1; then
    sed -i "s#http://127.0.0.1:8000#http://127.0.0.1:${PORT}#g" "$ROOT/dispatch.yaml"
  fi
else
  echo "[i] dispatch.yaml 已存在，保留"
fi

mkdir -p "$ROOT/datas" "$ROOT/logs" "$ROOT/workspaces"

cat <<EOF

===============================================
 Cairn 准备完成
 启动:   bash scripts/start.sh
 检测:   bash scripts/check-env.sh
 配置:   启动后打开 http://127.0.0.1:${PORT}
         服务器设置 -> Agent 模型，填写 Base URL / 模型 / API Key
===============================================
EOF
