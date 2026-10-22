"""签到核心：站点基类、结果结构、HTTP 会话、cookie 解析、日志脱敏。

设计原则
--------
* 一个站点 = 一个 `Site` 子类，实现 `run()` 返回若干 `Line`；
* 站点自己抛异常就代表失败，由 `main.py` 统一捕获，不会因为一个站点崩掉整轮；
* 所有 HTTP 请求都显式设置 timeout，避免 GitHub Actions 里挂死。
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

from . import settings

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

TIMEOUT = 20


@dataclass
class Line:
    """一条可读的结果行，最终拼成“名称: 值”。"""

    name: str
    value: Any

    def render(self) -> str:
        text = "" if self.value is None else str(self.value)
        return f"{self.name}: {text}" if text else self.name


@dataclass
class AccountResult:
    site: str
    label: str = ""
    ok: bool = True
    lines: list[Line] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0

    def text(self) -> str:
        if not self.ok:
            return f"[失败] {self.error}"
        return "\n".join(line.render() for line in self.lines) or "执行完成"


class VerifyUnsupported(Exception):
    """站点没有实现在线检测，面板显示"暂不支持"。"""


class Site:
    """站点基类。子类需要覆盖 key / name 并实现 run()。"""

    key: str = ""            # config.json 里的 KEY，也是环境变量前缀
    name: str = ""           # 通知里显示的名字
    fields: tuple[str, ...] = ("cookie",)
    required: tuple[str, ...] = ("cookie",)
    doc: str = ""            # 一句话说明凭据怎么拿
    timeout: int = 300       # 单账号执行上限（秒），仅在支持 SIGALRM 的平台生效

    def __init__(self, account: dict, index: int = 0):
        self.account = {
            k: (v.strip() if isinstance(v, str) else v) for k, v in (account or {}).items()
        }
        self.index = index
        missing = [f for f in self.required if not self.account.get(f)]
        if missing:
            raise ValueError(f"缺少必填配置: {' / '.join(missing)}")
        self.label = str(self.account.get("name") or self.account.get("remark") or "")

    # ------------------------------------------------------------------ #
    # 子类实现
    # ------------------------------------------------------------------ #
    def run(self) -> list[Line]:
        raise NotImplementedError

    def verify(self) -> tuple[bool, str]:
        """只读地检查凭据是否还有效，不做任何有副作用的操作。

        返回 (是否有效, 说明文字)。站点没实现就抛 VerifyUnsupported，
        面板会显示"该站点暂不支持在线检测"，不会假装成功或失败。
        """
        raise VerifyUnsupported("该站点暂不支持在线检测，点“立即签到”看结果即可")

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @property
    def cookie(self) -> str:
        return str(self.account.get("cookie") or "")

    def session(self, headers: dict | None = None, cookie: str | None = None) -> requests.Session:
        s = requests.Session()
        base = {"User-Agent": DEFAULT_UA}
        if headers:
            base.update(headers)
        s.headers.update(base)
        raw = self.cookie if cookie is None else cookie
        if raw:
            s.cookies = requests.utils.cookiejar_from_dict(parse_cookie(raw))
        # 代理优先级：账号级 > 站点级（面板）> 全局（面板）> 环境变量 GLOBAL_PROXY
        proxy = (
            self.account.get("proxy")
            or settings.site_proxy(self.key)
            or settings.global_proxy()
            or os.environ.get("GLOBAL_PROXY")
            or ""
        )
        if proxy:
            s.proxies.update({"http": str(proxy), "https": str(proxy)})
        return s


# ---------------------------------------------------------------------- #
# cookie 解析：兼容分号串、多行、JSON、curl 头和 DevTools 表格粘贴
# ---------------------------------------------------------------------- #
def parse_cookie(raw: str) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]

    # JSON（Cookie-Editor 导出 / 存储式）
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            return {
                str(item.get("name")): str(item.get("value", ""))
                for item in data
                if isinstance(item, dict) and item.get("name")
            }
        except (ValueError, TypeError):
            pass

    # 表格粘贴：name<TAB>value
    if "\t" in text:
        out = {}
        for row in text.splitlines():
            cells = [c.strip() for c in row.split("\t") if c.strip()]
            if len(cells) >= 2:
                out[cells[0]] = cells[1]
        if out:
            return out

    out: dict[str, str] = {}
    for part in text.replace("\r\n", "\n").replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def with_retry(fn, times: int = 3, base_delay: float = 1.0, label: str = ""):
    """指数退避重试，最后一次仍失败则抛出异常。"""
    last = None
    for i in range(times):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i == times - 1:
                break
            time.sleep(base_delay * (2 ** i) + random.uniform(0, 0.5))
    raise last  # type: ignore[misc]


def mask(value: str, head: int = 4, tail: int = 4) -> str:
    text = str(value or "")
    if len(text) <= head + tail:
        return "*" * len(text)
    return f"{text[:head]}***{text[-tail:]}"


def redact(text: str, secrets: Iterable[str]) -> str:
    """把日志里的 cookie / 密码替换掉，防止 Actions 日志泄漏。"""
    out = str(text)
    for secret in secrets:
        if secret and len(str(secret)) > 6:
            out = out.replace(str(secret), mask(str(secret)))
    return out


def friendly_error(exc: BaseException) -> str:
    """把 requests 的底层异常翻译成能看懂的提示。"""
    from urllib.parse import urlparse

    from requests.exceptions import (
        ConnectionError as ReqConnectionError,
        ProxyError,
        SSLError,
        Timeout,
    )

    def host() -> str:
        request = getattr(exc, "request", None)
        url = getattr(request, "url", "") or ""
        return urlparse(url).hostname or "目标站点"

    if isinstance(exc, ProxyError):
        return f"代理不可用，连不上 {host()}（本地调试请关掉 http_proxy / 代理软件，Actions 环境不需要代理）"
    if isinstance(exc, Timeout):
        return f"请求 {host()} 超时，站点无响应"
    if isinstance(exc, SSLError):
        return f"访问 {host()} 时 TLS 握手失败，可能被代理或防火墙拦截"
    if isinstance(exc, ReqConnectionError):
        return f"无法连接 {host()}"
    if isinstance(exc, TimeoutError):
        return str(exc)
    return str(exc) or exc.__class__.__name__


def verify_account(site_cls, account: dict, index: int = 0) -> tuple[str, str]:
    """检测一个账号的凭据，返回 (状态, 说明)。

    状态取值：ok / bad / unknown / unsupported。
    网络类异常一律算 unknown —— 不能因为本机连不上就告诉用户 cookie 失效了。
    """
    try:
        site = site_cls(account, index)
    except Exception as exc:  # noqa: BLE001
        return "bad", f"配置不完整：{exc}"

    try:
        ok, message = site.verify()
    except VerifyUnsupported as exc:
        return "unsupported", str(exc)
    except Exception as exc:  # noqa: BLE001
        return "unknown", friendly_error(exc)
    return ("ok" if ok else "bad"), message
