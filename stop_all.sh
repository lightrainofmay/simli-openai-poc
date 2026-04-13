#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$BASE_DIR/.run"

stop_service() {
  local name="$1"
  local pid_file="$RUN_DIR/${name}.pid"

  if [[ ! -f "$pid_file" ]]; then
    echo "[$name] not running (no pid file)"
    return
  fi

  local pid
  pid="$(cat "$pid_file")"

  if kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid" >/dev/null 2>&1 || true
    sleep 0.3
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
    echo "[$name] stopped (pid=$pid)"
  else
    echo "[$name] stale pid file removed (pid=$pid)"
  fi

  rm -f "$pid_file"
}

stop_service "realtime"
stop_service "agent"

# Cleanup any stray workers / log pipes (duplicate dev runs, filtered pipelines)
pkill -f "livekit_simli_agent.py" >/dev/null 2>&1 || true
pkill -f "uvicorn realtime_server:app" >/dev/null 2>&1 || true

echo "All stop commands processed."

