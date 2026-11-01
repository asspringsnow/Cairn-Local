#!/usr/bin/env bash
# Cairn 一键启动 / 停止 / 状态 / 日志。
#   bash scripts/start.sh            # 启动
#   bash scripts/start.sh stop       # 停止
#   bash scripts/start.sh restart
#   bash scripts/start.sh status
#   bash scripts/start.sh logs [server|dispatch]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${CAIRN_HOST:-0.0.0.0}"
PORT="${CAIRN_PORT:-8000}"
CONFIG="$ROOT/dispatch.yaml"
LOG_DIR="$ROOT/logs"
SERVER_LOG="$LOG_DIR/server.log"
DISPATCH_LOG="$LOG_DIR/dispatch.log"
SERVER_PID="$LOG_DIR/server.pid"
DISPATCH_PID="$LOG_DIR/dispatch.pid"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac

mkdir -p "$LOG_DIR" "$ROOT/datas" "$ROOT/workspaces"
cd "$ROOT"

is_alive() {
  local pf="$1"
  [ -f "$pf" ] || return 1
  local pid
  pid="$(cat "$pf" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

wait_health() {
  local i
  for i in $(seq 1 40); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

lan_ip() {
  hostname -I 2>/dev/null | awk '{print $1}'
}

start_one() {
  local pidfile="$1" logfile="$2"
  shift 2
  if command -v setsid >/dev/null 2>&1; then
    setsid "$@" >>"$logfile" 2>&1 </dev/null &
  else
    nohup "$@" >>"$logfile" 2>&1 </dev/null &
  fi
  echo $! >"$pidfile"
}

stop_one() {
  local name="$1" pidfile="$2"
  if [ ! -f "$pidfile" ]; then
    echo "[i] $name 未运行"
    return 0
  fi
  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pidfile"
    echo "[i] $name 未运行"
    return 0
  fi
  local pgid=""
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  if [ -n "$pgid" ] && [ "$pgid" = "$pid" ]; then
    kill -TERM -- "-$pid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi
  local i
  for i in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    if [ -n "$pgid" ] && [ "$pgid" = "$pid" ]; then
      kill -KILL -- "-$pid" 2>/dev/null || true
    else
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pidfile"
  echo "[+] 已停止 $name (pid $pid)"
}

cmd_start() {
  if ! command -v uv >/dev/null 2>&1; then
    echo "[!] 找不到 uv，先运行 bash scripts/setup.sh"
    exit 1
  fi
  if [ ! -f "$CONFIG" ]; then
    echo "[!] 缺少 $CONFIG"
    echo "    先运行 bash scripts/setup.sh"
    exit 1
  fi

  if is_alive "$SERVER_PID"; then
    echo "[i] server 已在运行（pid $(cat "$SERVER_PID")）"
  else
    echo "[*] 启动 server: $HOST:$PORT"
    start_one "$SERVER_PID" "$SERVER_LOG" \
      uv run --project "$ROOT/cairn" cairn serve \
      --host "$HOST" --port "$PORT" --db-path "$ROOT/datas/cairn.db" --no-access-log
    if wait_health; then
      echo "[+] server 就绪"
    else
      echo "[!] server 启动失败，日志：$SERVER_LOG"
      tail -20 "$SERVER_LOG" || true
      exit 1
    fi
  fi

  if is_alive "$DISPATCH_PID"; then
    echo "[i] dispatcher 已在运行（pid $(cat "$DISPATCH_PID")）"
  else
    echo "[*] 启动 dispatcher"
    start_one "$DISPATCH_PID" "$DISPATCH_LOG" \
      uv run --project "$ROOT/cairn" cairn dispatch --config "$CONFIG"
    sleep 3
    if ! is_alive "$DISPATCH_PID"; then
      echo "[!] dispatcher 启动失败，日志：$DISPATCH_LOG"
      tail -30 "$DISPATCH_LOG" || true
      exit 1
    fi
    echo "[+] dispatcher 已启动"
  fi

  echo
  echo "==============================================="
  echo " 本机:   http://127.0.0.1:${PORT}"
  ip="$(lan_ip || true)"
  [ -n "$ip" ] && echo " 局域网: http://${ip}:${PORT}"
  echo " 配置:   打开上面的地址 -> 服务器设置 -> Agent 模型"
  echo " 警告:   默认监听 0.0.0.0，Cairn 无内置认证，仅限授权内网/比赛虚拟机使用"
  echo " 日志:   $SERVER_LOG / $DISPATCH_LOG"
  echo " 停止:   bash scripts/start.sh stop"
  echo "==============================================="
}

cmd_stop() {
  stop_one "dispatcher" "$DISPATCH_PID"
  stop_one "server" "$SERVER_PID"
}

cmd_status() {
  local code=1
  if is_alive "$SERVER_PID"; then echo "[+] server     running (pid $(cat "$SERVER_PID"))"; code=0; else echo "[-] server     stopped"; fi
  if is_alive "$DISPATCH_PID"; then echo "[+] dispatcher running (pid $(cat "$DISPATCH_PID"))"; code=0; else echo "[-] dispatcher stopped"; fi
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    echo "[+] HTTP       http://127.0.0.1:${PORT}/"
  else
    echo "[-] HTTP       不可达"
  fi
  if command -v ss >/dev/null 2>&1; then
    echo "    监听: $(ss -ltnp 2>/dev/null | grep ":${PORT}" | head -2 || echo 未监听)"
  fi
  return $code
}

cmd_logs() {
  local which="${1:-all}"
  case "$which" in
    server) tail -n 80 -f "$SERVER_LOG" ;;
    dispatch) tail -n 80 -f "$DISPATCH_LOG" ;;
    *) tail -n 40 "$SERVER_LOG" 2>/dev/null; echo "-----"; tail -n 40 "$DISPATCH_LOG" 2>/dev/null ;;
  esac
}

case "${1:-start}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; sleep 1; cmd_start ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "${1:-all}" ;;
  *)
    echo "用法: bash scripts/start.sh {start|stop|restart|status|logs [server|dispatch]}"
    exit 1
    ;;
esac
