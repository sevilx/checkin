#!/bin/sh
# 容器入口：
#   RUN_AT 为空   → 单次执行后退出（适合外部 cron / 手动触发）
#   RUN_AT 有值   → 常驻，每天该时刻执行；PANEL_ENABLED != false 时同时起网页面板
set -u

RUN_AT="${RUN_AT-09:10}"
RUN_ON_START="${RUN_ON_START:-false}"
PANEL_ENABLED="${PANEL_ENABLED:-true}"
PANEL_PORT="${PANEL_PORT:-8080}"

if [ -z "$RUN_AT" ]; then
  echo "[entrypoint] RUN_AT 为空，单次执行后退出"
  exec python3 main.py
fi

if [ "$PANEL_ENABLED" != "false" ]; then
  echo "[entrypoint] 启动网页面板，端口 ${PANEL_PORT}"
  python3 -m panel.server &
  PANEL_PID=$!
  ( wait "$PANEL_PID"; echo "[entrypoint] 面板进程已退出（code=$?），签到调度继续运行" ) &
fi

if [ "$RUN_ON_START" = "true" ]; then
  echo "[entrypoint] RUN_ON_START=true，先跑一次"
  python3 main.py || true
fi

echo "[entrypoint] 计划每天 ${RUN_AT}（容器时区）执行签到"
while true; do
  now="$(date +%H:%M)"
  if [ "$now" = "$RUN_AT" ]; then
    echo "[entrypoint] ${now} 开始执行"
    python3 main.py || true
    sleep 90
  fi
  sleep 20
done
