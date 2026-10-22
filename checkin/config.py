"""配置加载：环境变量（GitHub Secrets）+ 本地 config.json（NAS / 本机）。

两种来源，同一套 KEY，来源优先级：环境变量 > config.json。

* 环境变量（推荐给 GitHub Actions）
    V2EX_COOKIE=xxx
    V2EX_COOKIE_1=xxx   V2EX_COOKIE_2=yyy     # 多账号
    V2EX_PROXY=http://192.168.1.2:7890         # 站点级代理（所有站点都支持）
    GLOBAL_PROXY=http://192.168.1.2:7890       # 全局代理，账号没单独配时生效
    TY189_USERNAME=138xxx  TY189_PASSWORD=xxx
    ENABLED_SITES=v2ex,nodeseek                 # 可选，留空=跑所有已配置站点

* config.json（直接复用 dailycheckin-plus 的 config/config.json，KEY 一致）
    { "V2EX": [{"cookie": "..."}], "TY189": [{"userName": "..."}] }
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"
ENV_FILE = ROOT / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """极简 .env 解析，不覆盖已存在的环境变量，纯标准库实现。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_file_config(path: Path = CONFIG_FILE) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"[config] config.json 解析失败，已忽略: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _env_accounts(prefix: str, fields: tuple[str, ...]) -> list[dict]:
    """从环境变量收集账号，支持 PREFIX_FIELD 与 PREFIX_FIELD_N 两种写法。"""
    env = os.environ
    buckets: dict[int, dict] = {}

    for field in fields:
        value = env.get(f"{prefix}_{field.upper()}")
        if value:
            buckets.setdefault(0, {})[field] = value

    pattern = re.compile(rf"^{re.escape(prefix)}_([A-Z0-9]+)_(\d+)$")
    known = {f.lower() for f in fields}
    for key, value in env.items():
        match = pattern.match(key)
        if not match or not value:
            continue
        field = match.group(1).lower()
        if field in known:
            buckets.setdefault(int(match.group(2)), {})[field] = value

    accounts = [buckets[i] for i in sorted(buckets)]

    # 高级写法：整段 JSON
    raw = env.get(f"{prefix}_ACCOUNTS")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            if isinstance(data, list):
                accounts = [d for d in data if isinstance(d, dict)] + accounts
        except ValueError:
            print(f"[config] {prefix}_ACCOUNTS 不是合法 JSON，已忽略")
    return accounts


def collect_accounts(site_cls, file_cfg: dict) -> list[dict]:
    """收集某站点的账号。

    proxy 与 name（备注）对所有站点通用，不需要每个模块自己声明：
        V2EX_PROXY=http://...      V2EX_NAME=主号
        V2EX_COOKIE_2=...          V2EX_NAME_2=小号
    """
    fields = tuple(site_cls.fields) + ("proxy", "name")
    env_accounts = _env_accounts(site_cls.key, fields)
    if env_accounts:
        return env_accounts

    raw = file_cfg.get(site_cls.key)
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def enabled_sites(file_cfg: dict | None = None) -> list[str] | None:
    """返回 ENABLED_SITES 指定的小写站点 key 列表；未设置返回 None。"""
    raw = os.environ.get("ENABLED_SITES", "").strip()
    if not raw:
        return None
    return [item.strip().lower() for item in re.split(r"[,\s]+", raw) if item.strip()]
