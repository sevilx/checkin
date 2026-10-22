"""面板可编辑的配置：`data/settings.json`。

优先级（同一站点）：**面板设置 > 环境变量 > config.json**。
面板里保存过一次，该站点就完全由面板接管，`.env` 里的同名配置不再生效 —— 这是为了让
"在网页上改 cookie" 真的能立刻生效，代价是两处都写时容易困惑，所以面板上会标出配置来源。

文件里是明文凭据（面板要能把值填回请求里），因此权限固定 0600，
且只存在于挂载出去的 `./data`，不会进 git、不会进镜像。
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from . import store

FILENAME = "settings.json"
VERSION = 1


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
# 读取
# ---------------------------------------------------------------------- #
def site_accounts(key: str) -> list[dict]:
    """面板里保存的账号列表；没配过返回空列表。"""
    sites = load().get("sites") or {}
    raw = sites.get(str(key).upper())
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item]


def global_proxy() -> str:
    proxy = load().get("proxy")
    if not isinstance(proxy, dict):
        return ""
    return str(proxy.get("global") or "").strip()


def site_proxy(key: str) -> str:
    proxy = load().get("proxy")
    if not isinstance(proxy, dict):
        return ""
    sites = proxy.get("sites")
    if not isinstance(sites, dict):
        return ""
    return str(sites.get(str(key).upper()) or "").strip()


# ---------------------------------------------------------------------- #
# 写入（由面板调用）
# ---------------------------------------------------------------------- #
def set_site(key: str, accounts: list[dict]) -> None:
    data = load()
    sites = data.get("sites")
    if not isinstance(sites, dict):
        sites = {}
    if accounts:
        sites[str(key).upper()] = accounts
    else:
        sites.pop(str(key).upper(), None)
    data["sites"] = sites
    save(data)


def set_proxies(global_proxy_value: str | None = None, sites: dict | None = None) -> None:
    data = load()
    proxy = data.get("proxy")
    if not isinstance(proxy, dict):
        proxy = {}
    if global_proxy_value is not None:
        proxy["global"] = str(global_proxy_value).strip()
    if sites is not None:
        current = proxy.get("sites")
        if not isinstance(current, dict):
            current = {}
        for key, value in sites.items():
            value = str(value or "").strip()
            if value:
                current[str(key).upper()] = value
            else:
                current.pop(str(key).upper(), None)
        proxy["sites"] = current
    data["proxy"] = proxy
    save(data)
