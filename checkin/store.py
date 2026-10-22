"""运行结果持久化：给网页面板读的“最近一次结果 + 历史记录”。

只写展示需要的信息（站点名、账号备注、结果文字），**不写 cookie / 密码**。
文件权限尽量收到 0600，位置可用 CHECKIN_DATA_DIR 覆盖（Docker 里挂卷）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CHECKIN_DATA_DIR") or (ROOT / "data"))
STATUS_FILE = DATA_DIR / "status.json"
RUNS_FILE = DATA_DIR / "runs.json"
STATE_FILE = DATA_DIR / "run_state.json"

MAX_RUNS = 30


def ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, payload) -> None:
    ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _read(path: Path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def load_status() -> dict:
    return _read(STATUS_FILE, {"updated_at": None, "entries": [], "ok": 0, "total": 0})


def load_runs() -> list:
    data = _read(RUNS_FILE, [])
    return data if isinstance(data, list) else []


def save_results(results, elapsed: float = 0.0) -> dict:
    """results: list[AccountResult]。返回写入的 status 结构。"""
    now = datetime.now()
    entries = []
    for item in results:
        entries.append(
            {
                "site": item.site,
                "label": item.label,
                "ok": bool(item.ok),
                "lines": [{"name": line.name, "value": str(line.value)} for line in item.lines],
                "error": item.error,
                "elapsed": round(item.elapsed, 1),
            }
        )

    ok = sum(1 for e in entries if e["ok"])
    status = {
        "updated_at": now.isoformat(timespec="seconds"),
        "ok": ok,
        "total": len(entries),
        "elapsed": round(elapsed, 1),
        "entries": entries,
    }
    _atomic_write(STATUS_FILE, status)

    runs = load_runs()
    runs.insert(
        0,
        {
            "at": status["updated_at"],
            "ok": ok,
            "total": len(entries),
            "elapsed": round(elapsed, 1),
            "failed": [f"{e['site']}{'（' + e['label'] + '）' if e['label'] else ''}" for e in entries if not e["ok"]],
        },
    )
    _atomic_write(RUNS_FILE, runs[:MAX_RUNS])
    clear_run_state()
    return status


def set_run_state(payload: dict) -> None:
    _atomic_write(STATE_FILE, payload)


def get_run_state() -> dict:
    return _read(STATE_FILE, {})


def clear_run_state() -> None:
    try:
        STATE_FILE.unlink()
    except OSError:
        pass
