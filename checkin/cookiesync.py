"""浏览器 Cookie 同步层：`data/cookies.json`。

为什么单独一层
--------------
`.env` 要重启容器、config.json 要改文件、面板设置会"接管整个站点"，
而浏览器 cookie 是**会过期、需要频繁刷新**的东西，混在它们里面很难受：

* 存在这里只覆盖 `cookie` 这一个字段，不动 proxy / 备注 / 其他字段；
* 清空这个文件就回到 `.env` / 面板的配置，不破坏原有优先级；
* 每条记录带同步时间，面板能直接告诉你"这个 cookie 是 3 小时前的"，过期了能看出来。

优先级：**同步层 > 面板设置 > 环境变量 > config.json**（仅针对 cookie 字段）。

文件里是明文凭据，权限固定 0600，只存在于挂载出去的 `./data`，不进 git、不进镜像。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from . import store

FILENAME = "cookies.json"
VERSION = 1

# 超过这个时长的同步记录在面板上标黄：多数论坛的会话 cookie 撑不过一两天。
# 瑞数（52pojie）/ Cloudflare 站点尤其短，这里取一个通用的偏保守值。
STALE_HOURS = 48


def path():
    return store.DATA_DIR / FILENAME


def load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> dict:
    store.ensure_dir()
    payload = dict(data)
    payload["version"] = VERSION
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")

    target = path()
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, target)
    return payload


# ---------------------------------------------------------------------- #
# 读
# ---------------------------------------------------------------------- #
def _sites(data: dict | None = None) -> dict:
    sites = (load() if data is None else data).get("sites")
    return sites if isinstance(sites, dict) else {}


def entry(key: str) -> dict:
    """某个站点的同步记录；没有就返回空 dict。"""
    raw = _sites().get(str(key).upper())
    return raw if isinstance(raw, dict) and raw.get("cookie") else {}


def cookie_for(key: str) -> str:
    return str(entry(key).get("cookie") or "")


def has(key: str) -> bool:
    return bool(cookie_for(key))


def synced_at(key: str) -> str:
    return str(entry(key).get("at") or "")


def _age_hours(stamp: str):
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return round((datetime.now() - moment).total_seconds() / 3600, 1)


def _cookie_names(cookie: str) -> list[str]:
    from .core import parse_cookie

    return list(parse_cookie(cookie).keys())


def summary() -> dict:
    """给面板用的概览：不返回明文，只给状态。"""
    from .core import mask

    out: dict[str, dict] = {}
    for key, raw in _sites().items():
        if not isinstance(raw, dict) or not raw.get("cookie"):
            continue
        cookie = str(raw["cookie"])
        age = _age_hours(str(raw.get("at") or ""))
        names = _cookie_names(cookie)
        out[str(key).upper()] = {
            "at": str(raw.get("at") or ""),
            "source": str(raw.get("source") or ""),
            "age_hours": age,
            "stale": bool(age is not None and age > STALE_HOURS),
            "masked": mask(cookie),
            "bytes": len(cookie),
            "count": len(names),
            "names": names[:8],
        }
    return out


# ---------------------------------------------------------------------- #
# 写
# ---------------------------------------------------------------------- #
def put(items: dict[str, dict], source: str = "") -> dict:
    """写入一批同步结果。

    items: {KEY: {"cookie": "...", "expires": "2026-11-03T10:00:00" 或 None}}
    返回站点数量统计。
    """
    data = load()
    sites = data.get("sites")
    if not isinstance(sites, dict):
        sites = {}

    stamp = datetime.now().isoformat(timespec="seconds")
    written = 0
    for key, payload in (items or {}).items():
        key = str(key).upper()
        if isinstance(payload, str):
            payload = {"cookie": payload}
        if not isinstance(payload, dict):
            continue
        cookie = str(payload.get("cookie") or "").strip()
        if not cookie:
            continue
        sites[key] = {
            "cookie": cookie,
            "at": stamp,
            "source": str(payload.get("source") or source or ""),
            "expires": payload.get("expires") or None,
        }
        written += 1

    data["sites"] = sites
    save(data)
    return {"written": written, "updated_at": stamp}


def remove(keys) -> int:
    data = load()
    sites = data.get("sites")
    if not isinstance(sites, dict):
        return 0
    removed = 0
    for key in keys or []:
        if sites.pop(str(key).upper(), None) is not None:
            removed += 1
    data["sites"] = sites
    save(data)
    return removed


def clear() -> int:
    data = load()
    removed = len(_sites(data))
    data["sites"] = {}
    save(data)
    return removed


def purge_stale(hours: int = STALE_HOURS) -> int:
    """清掉太旧的记录（默认 48 小时以上），返回清理条数。"""
    data = load()
    sites = data.get("sites")
    if not isinstance(sites, dict):
        return 0
    removed = 0
    for key in list(sites):
        age = _age_hours(str((sites.get(key) or {}).get("at") or ""))
        if age is not None and age > hours:
            sites.pop(key, None)
            removed += 1
    if removed:
        data["sites"] = sites
        save(data)
    return removed


def expiry_hint(expires: str | None) -> str:
    """把 cookie 的过期时间翻译成"还剩多久"，给面板显示。"""
    if not expires:
        return "会话级（关浏览器就失效）"
    try:
        moment = datetime.fromisoformat(expires)
    except ValueError:
        return str(expires)
    delta = moment - datetime.now()
    if delta <= timedelta(0):
        return "已过期"
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)} 分钟后过期"
    if hours < 48:
        return f"{int(hours)} 小时后过期"
    return f"{int(hours // 24)} 天后过期"
