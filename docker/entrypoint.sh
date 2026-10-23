#!/bin/sh
# 容器入口：
#   RUN_AT 被显式设为空（RUN_AT=）→ 单次执行后退出，由外部 cron / 手动触发
#   其他情况                      → 常驻：先起网页面板，再由调度器按时间表执行
#
# 时间表来源（优先级从高到低）：面板「设置 → 定时运行」> RUN_AT 环境变量 > 默认 09:10
# 面板里改完 15 秒内生效，不需要重启容器。
set -u

PANEL_ENABLED="${PANEL_ENABLED:-true}"
PANEL_PORT="${PANEL_PORT:-8080}"

if [ "${RUN_AT+set}" = "set" ] && [ -z "${RUN_AT}" ]; then
  echo "[entrypoint] RUN_AT 为空，单次执行后退出"
  exec python3 main.py
fi

if [ "$PANEL_ENABLED" != "false" ]; then
  echo "[entrypoint] 启动网页面板，容器内端口 ${PANEL_PORT}（宿主机端口看 compose 里的 ports）"
  if [ -z "${PANEL_PASSWORD:-}" ]; then
    echo "[entrypoint] 没设置 PANEL_PASSWORD：首次启动会生成随机口令，往下翻本页日志搜“面板口令”"
  fi
  python3 -m panel.server &
  PANEL_PID=$!
  ( wait "$PANEL_PID"; echo "[entrypoint] 面板进程已退出（code=$?），签到调度继续运行" ) &
fi

exec python3 -m checkin.scheduler
