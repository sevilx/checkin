"""运行时间表：什么时候自动跑签到。

三个来源，优先级从高到低：

1. **面板设置** —— `data/settings.json` 的 `schedule` 段（网面上改的）
2. **环境变量** `RUN_AT` —— 逗号分隔，如 `09:10,21:30`
3. **默认** `09:10`

面板里保存过一次，就以面板为准（`.env` 的 `RUN_AT` 不再生效），
这样"在网页上改时间"才是真的改得动，而不是被容器环境变量压回去。
面板上会显示当前时间表来自哪里。

注意 `RUN_AT` 被显式设为空字符串（`RUN_AT=`）是另一种语义：不启用调度、
跑一次就退出，由外部 cron 驱动。这个分支在 `docker/entrypoint.sh` 里处理。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from . import settings

DEFAULT_TIMES = ("09:10",)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SPLIT_RE = re.compile(r"[,，;；\s]+")


def parse(raw) -> tuple[list[str], list[str]]:
    """解析时间表，返回 (合法时间点, 非法条目)。

    接受 `"09:10, 21:30"`、`["9:10", "21:30"]`、多行文本，统一成
    `["09:10", "21:30"]`，去重并排序。非法条目原样返回，给面板提示用。
    """
    if raw is None:
        return [], []
    if isinstance(raw, str):
        items = SPLIT_RE.split(raw)
    elif isinstance(raw, (list, tuple, set)):
        items = []
        for item in raw:
            items.extend(SPLIT_RE.split(str(item)))
    else:
        return [], [str(raw)]

    good: list[str] = []
    bad: list[str] = []
    for item in items:
        text = item.strip()
        if not text:
            continue
        match = TIME_RE.match(text)
        if match:
            good.append(f"{int(match.group(1)):02d}:{match.group(2)}")
        else:
            bad.append(text)
    return sorted(set(good)), bad


def normalize(raw) -> list[str]:
    return parse(raw)[0]


def load() -> dict:
    """当前生效的时间表：{enabled, times, source}。"""
    panel = settings.load().get("schedule")
    if isinstance(panel, dict) and ("times" in panel or "enabled" in panel):
        times = normalize(panel.get("times"))
        return {
            "enabled": bool(panel.get("enabled", True)) and bool(times),
            "times": times,
            "source": "panel",
        }

    raw = os.environ.get("RUN_AT")
    if raw is not None:
        times = normalize(raw)
        return {"enabled": bool(times), "times": times, "source": "env"}

    return {"enabled": True, "times": list(DEFAULT_TIMES), "source": "default"}


def save(enabled: bool, raw_times) -> dict:
    """写进面板配置；返回保存后实际生效的时间表。"""
    times = normalize(raw_times)
    data = settings.load()
    data["schedule"] = {
        "enabled": bool(enabled) and bool(times),
        "times": times,
    }
    settings.save(data)
    return load()


def next_run(cfg: dict | None = None, now: datetime | None = None) -> datetime | None:
    """下一次触发时刻；未启用或没有时间点返回 None。"""
    cfg = load() if cfg is None else cfg
    if not cfg.get("enabled") or not cfg.get("times"):
        return None
    now = datetime.now() if now is None else now
    for day_offset in (0, 1):
        base = (now + timedelta(days=day_offset)).replace(second=0, microsecond=0)
        for item in cfg["times"]:
            hour, minute = (int(part) for part in item.split(":"))
            candidate = base.replace(hour=hour, minute=minute)
            if candidate > now:
                return candidate
    return None


def describe(cfg: dict) -> str:
    """给日志用的一句话。"""
    if not cfg.get("enabled") or not cfg.get("times"):
        return "未启用自动运行"
    source = {"panel": "面板设置", "env": "RUN_AT 环境变量", "default": "默认值"}.get(
        cfg.get("source", ""), cfg.get("source", "")
    )
    return f"每天 {'、'.join(cfg['times'])}（来源：{source}）"
