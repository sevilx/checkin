"""常驻调度器：到点就执行一次 `main.py`。

为什么不用系统 cron / sleep 固定间隔：

* cron 读不到面板里改的时间，改完必须重启容器；
* 固定 24 小时 sleep 会漂移，且容器重启后要重新对齐。

这里每 15 秒重新读一次时间表（`checkin/schedule.py`），所以：

* **面板里改时间，15 秒内生效，不用重启容器**；
* 改完会立刻打印新旧计划，日志里能对照；
* 已经跑过的时间点记在 `data/schedule_state.json`，容器重启不会重复跑；
* 和面板的「立即签到」共用同一把锁，不会同时跑两轮。

单独用一次（外部 cron 调用）：

    python3 -m checkin.scheduler --once
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import schedule, store

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = "schedule_state.json"
TICK = 15.0


def _log(message: str) -> None:
    print(f"[scheduler] {message}", flush=True)


def _read_state() -> dict:
    try:
        data = json.loads((store.DATA_DIR / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(payload: dict) -> None:
    store.ensure_dir()
    path = store.DATA_DIR / STATE_FILE
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def run_checkin(source: str) -> int:
    """跑一轮，返回退出码；已有任务在跑则返回 -1。"""
    if not store.acquire_run_lock():
        _log("已有一轮在运行，跳过本次")
        return -1

    store.set_run_state(
        {"started_at": datetime.now().isoformat(timespec="seconds"), "source": source}
    )
    env = os.environ.copy()
    env["CHECKIN_DATA_DIR"] = str(store.DATA_DIR)
    store.ensure_dir()
    log_path = store.DATA_DIR / "last-run.log"

    try:
        with open(log_path, "wb") as log:
            code = subprocess.call(
                [sys.executable, "-u", "main.py"],
                cwd=str(ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    except Exception as exc:  # noqa: BLE001
        _log(f"启动失败: {exc}")
        code = -1
    finally:
        store.release_run_lock()
        store.clear_run_state()
    return code


def _fire(now: datetime) -> None:
    cfg = schedule.load()
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")

    state = _read_state()
    fired = list(state.get("fired") or []) if state.get("date") == today else []

    _log(f"{hhmm} 到点，开始执行（计划：{', '.join(cfg['times'])}）")
    code = run_checkin("scheduler")
    if code == -1:
        return

    fired.append(hhmm)
    _write_state({"date": today, "fired": fired, "last_finished": datetime.now().isoformat(timespec="seconds")})
    _log(f"执行结束，退出码 {code}")

    nxt = schedule.next_run(cfg, datetime.now())
    if nxt:
        _log(f"下次运行 {nxt:%Y-%m-%d %H:%M}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    once = "--once" in argv

    if once:
        _log("单次模式：只跑一轮")
        return run_checkin("cli")

    now = datetime.now()
    _log(f"启动于 {now:%Y-%m-%d %H:%M}，时区 {time.strftime('%Z')}")

    if str(os.environ.get("RUN_ON_START", "")).lower() in ("1", "true", "yes", "on"):
        _log("RUN_ON_START 已开启，先跑一次")
        run_checkin("start")

    seen: tuple | None = None
    while True:
        cfg = schedule.load()
        now = datetime.now()
        signature = (cfg["enabled"], tuple(cfg["times"]), cfg["source"])

        if signature != seen:
            seen = signature
            if cfg["enabled"]:
                nxt = schedule.next_run(cfg, now)
                when = f"下次 {nxt:%Y-%m-%d %H:%M}" if nxt else "暂无下次"
                _log(f"计划：{schedule.describe(cfg)}，{when}")
            else:
                _log("计划：未启用自动运行（在面板「设置 → 定时运行」里开启）")

        if cfg["enabled"] and now.strftime("%H:%M") in cfg["times"]:
            today = now.strftime("%Y-%m-%d")
            state = _read_state()
            fired = list(state.get("fired") or []) if state.get("date") == today else []
            if now.strftime("%H:%M") not in fired:
                _fire(now)
                seen = None  # 跑完重新计算下次时间
        time.sleep(TICK)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
