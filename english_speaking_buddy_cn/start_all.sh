#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$BASE_DIR/.run"
LOG_DIR="$BASE_DIR/.logs"
VENV_PATH="${VENV_PATH:-/Users/apple/Documents/Playground/.venv}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

if [[ ! -f "$VENV_PATH/bin/activate" ]]; then
  echo "Virtualenv not found: $VENV_PATH"
  echo "Use: VENV_PATH=/path/to/venv ./start_all.sh"
  exit 1
fi

start_service() {
  local name="$1"
  local command="$2"
  local pid_file="$RUN_DIR/${name}.pid"
  local log_file="$LOG_DIR/${name}.log"

  if [[ -f "$pid_file" ]]; then
    local old_pid
    old_pid="$(cat "$pid_file")"
    if kill -0 "$old_pid" >/dev/null 2>&1; then
      echo "[$name] already running (pid=$old_pid)"
      return
    else
      rm -f "$pid_file"
    fi
  fi

  nohup bash -lc "cd \"$BASE_DIR\" && source \"$VENV_PATH/bin/activate\" && $command" >"$log_file" 2>&1 &
  local pid=$!
  echo "$pid" >"$pid_file"
  echo "[$name] started (pid=$pid) log=$log_file"
}

# `dev`: 本地开发 + 热重载，日志进 .logs/agent.log（可能有大量 \\0，不影响运行）
# 生产部署用: python3 livekit_simli_agent.py start
start_service "agent" "python3 livekit_simli_agent.py dev"
start_service "realtime" "uvicorn realtime_server:app --host 127.0.0.1 --port 8030"

echo ""
echo "Services started."
echo "Realtime page: http://127.0.0.1:8030"
echo ""
echo "Tail logs:"
echo "  tail -f .logs/agent.log"
echo "  tail -f .logs/realtime.log"
echo ""
echo "Stop all:"
echo "  ./stop_all.sh"
