#!/usr/bin/env bash
# 与 ./start_all.sh 相同（均为 dev 热重载）
exec "$(cd "$(dirname "$0")" && pwd)/start_all.sh"
