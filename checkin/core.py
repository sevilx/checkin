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

# curl_cffi 是可选依赖：它用 BoringSSL 复刻 Chrome / Safari 的 TLS(JA3) + HTTP/2 指纹。
# 像 NodeSeek、LinuxDo 这种挂了 Cloudflare 的站点，光拼请求头没用 —— Cloudflare 会先看
# TLS 握手长什么样，requests 的握手特征太"像脚本"，直接挑战或 403。
try:  # pragma: no cover - 取决于运行环境是否装了可选依赖
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:  # noqa: BLE001
    cffi_requests = None


def has_curl_cffi() -> bool:
    return cffi_requests is not None


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

TIMEOUT = 20

# 真实浏览器会带的一组请求头。Cloudflare 之类的风控会看这些字段，
# 只发 User-Agent 的裸请求很容易被判定成脚本，直接 403。
_CH_UA = '"Chromium";v="141", "Google Chrome";v="141", "Not?A_Brand";v="24"'

NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Sec-CH-UA": _CH_UA,
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

XHR_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Sec-CH-UA": _CH_UA,
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Cloudflare / WAF 拦截页的特征串
_BLOCK_MARKERS = (
    "just a moment",
    "attention required",
    "cf-error-details",
    "cf_chl_opt",
    "cloudflare",
    "enable javascript and cookies",
    "access denied",
)


# 绑定到「出口 IP + User-Agent」的 Cloudflare 会话 cookie。
# 浏览器里可能有效，但从 NAS / Actions 发出去时用的是另一个 IP 和另一个指纹，
# 带着它们反而更容易被判成"拿着别人票据的请求"，所以默认丢掉。
CF_COOKIES = (
    "cf_clearance",
    "__cf_bm",
    "_cfuvid",
    "cf_chl_2",
    "cf_chl_prog",
    "cf_chl_1",
    "cf_ob_info",
    "cf_use_ob",
)


def looks_blocked(status: int, text: str, headers=None) -> bool:
    """判断这次响应是不是被 WAF / Cloudflare 拦了，而不是站点真的出错。

    `cf-mitigated: challenge` 是 Cloudflare 自己打的标记，比正文关键字可靠得多，
    优先级最高。
    """
    if headers is not None:
        try:
            if str(headers.get("cf-mitigated") or "").lower() == "challenge":
                return True
        except Exception:  # noqa: BLE001
            pass
    if status not in (403, 429, 503):
        return False
    body = (text or "")[:4000].lower()
    return any(marker in body for marker in _BLOCK_MARKERS)


def cookie_string(cookies: dict[str, str]) -> str:
    """把 cookie 字典拼回 `a=1; b=2`，顺序保持原始插入顺序。"""
    return "; ".join(f"{key}={value}" for key, value in cookies.items() if key)


def _fingerprint_headers(headers: dict | None, browser: str) -> dict[str, str]:
    """指纹模式下只保留"业务头"，浏览器特征头交给 curl_cffi 自己发。

    Safari 压根不发 Sec-CH-UA / Sec-Fetch-*，如果硬塞一套 Chrome 的 client hints，
    "Safari 的 TLS 握手 + Chrome 的客户端提示" 反而比什么都不塞更可疑，所以按指纹类型裁剪。
    """
    safari = "safari" in str(browser).lower()
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        low = key.lower()
        if low.startswith("sec-ch-"):
            continue
        if safari and low.startswith("sec-fetch-"):
            continue
        out[key] = value
    return out


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

    # 挂了 Cloudflare 的站点：依次尝试的浏览器指纹（curl_cffi 的 impersonate 值）。
    # 空元组表示这个站点不需要（普通 requests 就够）。
    cf_impersonates: tuple[str, ...] = ()

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

    def session(self, headers: dict | None = None, cookie: str | None = None, browser: str | None = None):
        """建一个 HTTP 会话。

        browser 传 curl_cffi 的指纹名（如 `safari15_5`）时，会话会模仿对应浏览器的
        TLS / HTTP2 握手，用来过 Cloudflare 的被动指纹检查；没装 curl_cffi 就自动退回
        requests，不会因为少个可选依赖直接崩掉。
        """
        raw = self.cookie if cookie is None else cookie
        proxy = (
            self.account.get("proxy")
            or settings.site_proxy(self.key)
            or settings.global_proxy()
            or os.environ.get("GLOBAL_PROXY")
            or ""
        )

        if browser and cffi_requests is not None:
            try:
                sess = cffi_requests.Session(impersonate=browser)
            except TypeError:  # 老版本 curl_cffi 不认构造参数，退一步用属性设置
                sess = cffi_requests.Session()
                sess.impersonate = browser
            extra: dict[str, str] = _fingerprint_headers(headers, browser)
            if raw:
                # 指纹模式下不用 cookie jar，直接给 Cookie 头：
                # 换域名的场景这里没有，反而省掉 jar 的 API 差异
                extra["Cookie"] = raw
            if extra:
                sess.headers.update(extra)
            if proxy:
                sess.proxies = {"http": str(proxy), "https": str(proxy)}
            return sess

        s = requests.Session()
        base = {"User-Agent": DEFAULT_UA}
        if headers:
            base.update(headers)
        s.headers.update(base)
        if raw:
            s.cookies = requests.utils.cookiejar_from_dict(parse_cookie(raw))
        # 代理优先级：账号级 > 站点级（面板）> 全局（面板）> 环境变量 GLOBAL_PROXY
        if proxy:
            s.proxies.update({"http": str(proxy), "https": str(proxy)})
        return s

    # ------------------------------------------------------------------ #
    # Cloudflare 站点专用
    # ------------------------------------------------------------------ #
    def impersonates(self) -> tuple[str | None, ...]:
        """可用的指纹列表；没装 curl_cffi 时只跑一次（用 requests）。"""
        if cffi_requests is None:
            return (None,)
        return tuple(self.cf_impersonates) or (None,)

    def cf_cookies(self) -> tuple[str, bool]:
        """返回 (给 Cloudflare 站点用的 Cookie 串, 是否保留了 cf_clearance)。

        默认把 cf_clearance / __cf_bm / _cfuvid 这些「和 IP + UA 绑定」的 cookie 去掉：
        它们是从你电脑的浏览器、用你电脑的 IP 换来的，拿到 NAS 上发出去一定对不上，
        留着只会让 Cloudflare 更怀疑。想强行保留就配 `keep_cf=true`，
        此时还要用 `ua` 字段把浏览器 UA 一起写进来，否则同样对不上。
        """
        cookies = parse_cookie(self.cookie)
        keep = str(self.account.get("keep_cf") or "").strip().lower() in ("1", "true", "yes", "on")
        if keep:
            return cookie_string(cookies), bool(cookies.get("cf_clearance"))
        kept = {k: v for k, v in cookies.items() if k.lower() not in CF_COOKIES}
        return cookie_string(kept), False

    def cf_headers(self, headers: dict, kept_cf: bool = False) -> dict:
        """保留 cf_clearance 时，UA 必须和当初换这个 cookie 的浏览器一模一样。"""
        out = dict(headers)
        ua = str(self.account.get("ua") or "").strip()
        if ua and kept_cf:
            out["User-Agent"] = ua
        return out

    def browser_request(self, request, cookie: str | None = None, headers: dict | None = None):
        """依次换浏览器指纹执行 `request(session)`，直到不再被 Cloudflare 拦。

        返回 (response, session)。全都被拦时抛 RuntimeError，并把每种指纹的结果写进提示里，
        方便判断到底是 IP 的问题还是指纹的问题。
        """
        cookie_str = self.cookie if cookie is None else cookie
        blocked: list[tuple[str, int]] = []
        last_exc: BaseException | None = None
        for imp in self.impersonates():
            try:
                session = self.session(headers=headers, cookie=cookie_str, browser=imp)
                resp = request(session)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            if looks_blocked(resp.status_code, resp.text, resp.headers):
                blocked.append((imp or "requests", resp.status_code))
                continue
            return resp, session
        if blocked:
            raise RuntimeError(self.cf_hint(blocked))
        if last_exc is not None:
            # 全是网络层问题（连不上 / 代理挂 / 超时），原样抛出，
            # 让上层按"连不上"处理，而不是谎报成 cookie 失效
            raise last_exc
        raise RuntimeError("请求失败")

    def cf_hint(self, blocked: list[tuple[str, int]]) -> str:
        tried = "、".join(f"{name}→HTTP {code}" for name, code in blocked)
        missing = (
            "\n当前环境没装 curl_cffi，只能用 requests 的默认 TLS 指纹，Cloudflare 一眼就能认出来；"
            "装上就多一层保障：pip install curl_cffi（Docker 镜像里已经预装）"
            if cffi_requests is None
            else ""
        )
        return (
            f"被 Cloudflare 拦下来了（换了几种浏览器指纹都不行：{tried}）。\n"
            "这不是 cookie 的问题 —— 请求根本没到站点服务器，先看这三条：\n"
            "1) 出口 IP 最要紧：机房 IP、云服务器、部分机场节点会被直接挑战，换家宽或换节点；\n"
            "2) 别把浏览器里的 cf_clearance 抄进来：它和当时的 IP、UA 绑定，换台机器必然失效，"
            "本程序默认已经帮你丢掉了；\n"
            f"3) 还是不行，就在设置页给这个站点单独配代理，走和浏览器同一个出口。{missing}"
        )


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
