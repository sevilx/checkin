#!/usr/bin/env python3
"""容器内只读代理诊断：代理是否生效、出口 IP、LinuxDo 404 的性质。

只输出布尔值 / 状态码 / 长度 / 出口 IP，绝不打印 Cookie、请求体、站点正文内容。
代理地址只从 --proxy 参数或 DIAG_PROXY 环境变量读取，脚本内不含任何真实 IP / 端口 / 口令。

用法（在 NAS 的 checkin 容器里执行）：

    docker exec -it checkin python3 tools/diagnose_proxy.py \
        --proxy '<scheme>://<host>:<port>'

    # 用环境变量传，避免命令行里出现代理串
    docker exec -e DIAG_PROXY='<scheme>://<host>:<port>' checkin \
        python3 tools/diagnose_proxy.py

    # <scheme> 可为 socks5h://（域名交给代理解析）/ socks5:// / http:// / https://
    # 未传 --proxy 时依次读 DIAG_PROXY、GLOBAL_PROXY；两者都空则按直连诊断。

    # 带登录态只读探测（一条命令区分出口 / UA / cookie 的问题）：
    docker exec -it checkin python3 tools/diagnose_proxy.py --check-login

    # 指定指纹 / UA（两者都不是私密值），并打印 curl_cffi 版本与 2x2 对比：
    docker exec -it checkin python3 tools/diagnose_proxy.py --check-login \
        --impersonate firefox155 --ua '<浏览器 UA>'

脚本只读：不写文件、不改配置。默认不携带任何登录态（不构造 Cookie 头）；
仅显式传 --check-login 时，才用容器内同步层的 LinuxDo Cookie 发一组只读 GET
（单次探测 + 指纹 × cf_clearance 的 2x2 对比），且只打印状态码 / 是否 200 /
是否含 current_user / CF 特征，绝不打印 Cookie 值、请求体或站点正文内容。
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

# 允许从任意工作目录执行时都能 import 到项目自己的 CF 标记定义
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # pragma: no cover - 容器里预装，本地可能没有
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:  # noqa: BLE001
    cffi_requests = None

try:  # 与主程序共用同一套高特异性标记，避免诊断结论和拦截判定不一致
    from checkin.core import (
        CF_COOKIES,
        _HIGH_SPECIFIC_BLOCK_MARKERS as CF_MARKERS,
        cookie_string,
        parse_cookie,
    )
except Exception:  # noqa: BLE001
    CF_MARKERS = (
        "cf-error-details",
        "attention required",
        "just a moment",
        "enable javascript and cookies",
        "cf_chl_opt",
        "cf_clearance",
    )
    CF_COOKIES = (
        "cf_clearance", "__cf_bm", "_cfuvid", "cf_chl_2",
        "cf_chl_prog", "cf_chl_1", "cf_ob_info", "cf_use_ob",
    )

    def parse_cookie(raw: str) -> dict[str, str]:
        """core.parse_cookie 的最小兜底，只读诊断不依赖它也要能跑。"""
        out: dict[str, str] = {}
        for part in str(raw or "").replace("\n", ";").split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key:
                out[key.strip()] = value.strip()
        return out

    def cookie_string(cookies: dict[str, str]) -> str:
        return "; ".join(f"{key}={value}" for key, value in cookies.items() if key)

IP_URL = "https://api.ipify.org?format=json"
LINUXDO_SESSION_URL = "https://linux.do/session/current.json"
IMPERSONATE = "chrome120"
# 2x2 对比固定用这两个指纹：Firefox 家族 vs 站点默认 Chrome，覆盖"家族矛盾"假设。
MATRIX_IMPERSONATES = ("firefox155", "chrome120")
TIMEOUT = 12


def _yes(value: bool) -> str:
    return "YES" if value else "NO"


def _safe_error(exc: BaseException) -> str:
    """异常文本可能带完整代理 URL（含口令），只回异常类名。"""
    return type(exc).__name__


def _parse_args():
    """解析命令行参数；--check-login 默认关闭，老用法行为完全不变。"""
    parser = argparse.ArgumentParser(
        description="只读诊断代理连通性、出口 IP 与 LinuxDo 404 性质（不打印凭据 / 正文）。"
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="代理地址，如 <scheme>://<host>:<port>（请写出端口）；缺省时读 DIAG_PROXY / GLOBAL_PROXY 环境变量",
    )
    parser.add_argument(
        "--check-login",
        action="store_true",
        help="额外用容器内同步层的 LinuxDo Cookie 只读 GET /session/current.json（不打印 Cookie / 正文）",
    )
    parser.add_argument(
        "--impersonate",
        default=IMPERSONATE,
        help=f"单次带登录态探测用的 curl_cffi 指纹（非私密值，默认 {IMPERSONATE}）",
    )
    parser.add_argument(
        "--ua",
        default="",
        help="单次探测注入的 User-Agent（非私密值；与站点 ua 字段一致才复现真实请求）",
    )
    return parser.parse_args()


def _proxy_from_args(args=None) -> str:
    """取代理串：--proxy > DIAG_PROXY > GLOBAL_PROXY；保留旧调用方式。"""
    args = args if args is not None else _parse_args()
    raw = str(
        getattr(args, "proxy", "")
        or os.environ.get("DIAG_PROXY")
        or os.environ.get("GLOBAL_PROXY")
        or ""
    )
    return raw.strip()


def _endpoint(proxy: str) -> tuple[str, int]:
    """从代理串里取 host/port，绝不返回 userinfo；端口必须显式给出。"""
    parsed = urlparse(proxy if "://" in proxy else "http://" + proxy)
    host = parsed.hostname or ""
    try:
        port = int(parsed.port or 0)
    except ValueError:
        port = 0
    return host, port


def _describe_proxy(proxy: str) -> str:
    parsed = urlparse(proxy if "://" in proxy else "http://" + proxy)
    host, port = _endpoint(proxy)
    return (
        f"scheme={parsed.scheme or '-'} host={host or '-'} port={port or '-'}"
    )


def _check_env() -> None:
    print("== 环境变量 ==")
    for name in ("DIAG_PROXY", "GLOBAL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        print(f"  {name}: {_yes(bool(os.environ.get(name)))}")
    try:
        import socks  # noqa: F401  # PySocks
        print("  PySocks: INSTALLED")
    except Exception:  # noqa: BLE001
        print("  PySocks: MISSING (requests 分支的 socks5h:// 代理会静默失效)")


def _check_tcp(proxy: str) -> None:
    host, port = _endpoint(proxy)
    if not host or not port:
        print("  TCP 连通: SKIP (代理地址缺 host 或 port)")
        return
    try:
        with socket.create_connection((host, port), timeout=5):
            print(f"  TCP 连通: YES (host={host}, port={port})")
    except Exception as exc:  # noqa: BLE001
        print(f"  TCP 连通: NO (host={host}, port={port}, {_safe_error(exc)})")


def _new_session(proxy_map: dict[str, str] | None, mode: str, impersonate: str = IMPERSONATE):
    """mode: direct / ctor / post；与 core.session() 的三种 curl_cffi 模式对齐。"""
    if cffi_requests is None:
        raise RuntimeError("curl_cffi missing")
    if proxy_map and mode == "ctor":
        try:
            return cffi_requests.Session(impersonate=impersonate, proxies=proxy_map)
        except TypeError:  # 老版本不认构造参数，退回属性赋值
            sess = cffi_requests.Session()
            sess.impersonate = impersonate
            sess.proxies = dict(proxy_map)
            return sess
    sess = cffi_requests.Session(impersonate=impersonate)
    if proxy_map and mode == "post":
        sess.proxies = dict(proxy_map)
    return sess


def _check_exit_ip(proxy_map: dict[str, str] | None, mode: str) -> None:
    label = {
        "direct": "direct",
        "ctor": "ctor-proxies",
        "post": "post-assign",
    }.get(mode, mode)
    try:
        session = _new_session(proxy_map, mode=mode)
        resp = session.get(IP_URL, timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        print(f"  出口 IP [{label}]: FAIL ({_safe_error(exc)})")
        return
    body = (resp.text or "").strip()
    ip = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            ip = str(data.get("ip") or "")
    except ValueError:
        ip = body
    if not re.fullmatch(r"[0-9a-fA-F:.]+", ip):
        ip = "(unparsed)"
    print(f"  出口 IP [{label}]: HTTP {resp.status_code} ip={ip}")


def _check_linuxdo(proxy_map: dict[str, str] | None) -> None:
    """匿名访问 /session/current.json；只打印特征和长度，不打印正文。"""
    try:
        session = _new_session(proxy_map, mode="ctor" if proxy_map else "direct")
        resp = session.get(LINUXDO_SESSION_URL, timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        print(f"  LinuxDo /session/current.json: FAIL ({_safe_error(exc)})")
        return
    headers = resp.headers or {}
    body = (resp.text or "")[:4000].lower()
    cf_body = any(marker in body for marker in CF_MARKERS)
    mitigated = bool(headers.get("cf-mitigated"))
    ray = bool(headers.get("cf-ray"))
    server_exist = bool(headers.get("server"))
    server_cf = "cloudflare" in str(headers.get("server") or "").lower()
    has_ct = bool(headers.get("content-type"))
    current_user = "current_user" in body
    error_type = "error_type" in body
    print(
        f"  LinuxDo /session/current.json: HTTP {resp.status_code} body_len={len(resp.text or '')} "
        f"cf-ray={_yes(ray)} cf-mitigated={_yes(mitigated)} "
        f"server={_yes(server_exist)} server_cloudflare={_yes(server_cf)} "
        f"content-type={_yes(has_ct)} body_cf_marker={_yes(cf_body)} current_user={_yes(current_user)}"
    )
    if resp.status_code in (401, 403, 404):
        if cf_body or mitigated:
            print("  结论: 命中 CF 挑战特征 —— 按出口 IP / 代理节点排查（CF 可能用 404 当拦截页）。")
        elif current_user or error_type:
            print("  结论: 像 Discourse 未登录的正常 404 JSON，不是 CF 拦截。")
        else:
            print("  结论: 未见明确 CF 正文标记；若同时有 cf-ray / server=cloudflare，先换节点验证。")


def _check_login_state(
    proxy_map: dict[str, str] | None,
    impersonate: str = IMPERSONATE,
    ua: str = "",
) -> None:
    """带登录态只读 GET /session/current.json，一条命令区分出口 / UA / cookie。

    安全边界：
    * Cookie 只从 checkin.cookiesync.cookie_for("LINUXDO") 读，绝不打印其值；
    * 只打印状态码 / 布尔特征，不打印正文内容；
    * 容器内没有 data/cookies.json（或没有 LINUXDO 记录）时打印 SKIP，不崩；
    * 同步层不存 ua（ua 在面板/配置文件），默认用 curl_cffi 指纹自带 UA；
      显式传 --ua 时才注入（UA 本身不是私密值），用于复现真实请求。
    """
    try:
        from checkin.cookiesync import cookie_for
    except Exception as exc:  # noqa: BLE001
        print(f"  带登录态探测: SKIP ({_safe_error(exc)})：无法导入 checkin.cookiesync")
        return
    try:
        cookie = cookie_for("LINUXDO")
    except Exception as exc:  # noqa: BLE001
        print(f"  带登录态探测: SKIP ({_safe_error(exc)})：读取同步层 Cookie 失败")
        return
    if not cookie:
        print("  带登录态探测: SKIP（同步层 data/cookies.json 里没有 LINUXDO 的 Cookie）")
        return

    # 只判断有没有 cf_clearance 这个键，不显示任何 cookie 值 / 完整键名列表
    try:
        from checkin.core import parse_cookie

        names = parse_cookie(cookie).keys()
        has_cf_clearance = "cf_clearance" in names
    except Exception:  # noqa: BLE001
        has_cf_clearance = False
    print(f"  同步层 Cookie: 已读取（值不显示；含 cf_clearance={_yes(has_cf_clearance)}）")

    if cffi_requests is None:
        print("  带登录态探测: SKIP（curl_cffi 未安装，无法用与站点一致的指纹）")
        return
    headers = {"Cookie": cookie}
    if ua:
        headers["User-Agent"] = ua
    try:
        session = _new_session(
            proxy_map, mode="ctor" if proxy_map else "direct", impersonate=impersonate
        )
        resp = session.get(LINUXDO_SESSION_URL, headers=headers, timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        print(f"  带登录态探测: FAIL (impersonate={impersonate}, {_safe_error(exc)})")
        return
    print(f"  带登录态探测参数: impersonate={impersonate} ua_injected={_yes(bool(ua))}")

    resp_headers = resp.headers or {}
    body = (resp.text or "")[:4000].lower()
    status = resp.status_code
    cf_body = any(marker in body for marker in CF_MARKERS)
    mitigated = bool(resp_headers.get("cf-mitigated"))
    ray = bool(resp_headers.get("cf-ray"))
    server_cf = "cloudflare" in str(resp_headers.get("server") or "").lower()
    current_user = "current_user" in body
    error_type = "error_type" in body
    print(
        f"  带登录态 /session/current.json: HTTP {status} is_200={_yes(status == 200)} "
        f"current_user={_yes(current_user)} cf-ray={_yes(ray)} "
        f"cf-mitigated={_yes(mitigated)} server_cloudflare={_yes(server_cf)} "
        f"body_cf_marker={_yes(cf_body)}"
    )
    if status == 200 and current_user:
        print("  结论: 登录态有效（带 Cookie 能拿到 current_user）。")
    elif cf_body or mitigated:
        print("  结论: 命中 CF 挑战 —— 出口 IP / UA 与换取 cf_clearance 时不一致的可能性最大。")
    elif current_user or error_type:
        print("  结论: 站点正常返回但没有登录态 —— Cookie 本身可能失效（_t 过期 / 换设备）。")
    else:
        print("  结论: 未见明确特征；对比匿名探测结果，若两者不同说明 Cookie 生效但被站点拒绝。")
    print(
        "  提示: 未传 --ua 时本探测的 UA 来自 curl_cffi 指纹，不是面板的 ua；"
        "要完全复现站点请求请同时传 --impersonate 与 --ua（两者都不是私密值）。"
    )


def _check_login_matrix(proxy_map: dict[str, str] | None, ua: str = "") -> None:
    """只读 2x2 对比：firefox155/chrome120 × 保留/丢弃 cf_clearance。

    Cookie 永远只从同步层读，绝不回显；只打印状态码和布尔特征。
    """
    try:
        from checkin.cookiesync import cookie_for
    except Exception as exc:  # noqa: BLE001
        print(f"  2x2 对比: SKIP ({_safe_error(exc)})：无法导入 checkin.cookiesync")
        return
    try:
        cookie = cookie_for("LINUXDO")
    except Exception as exc:  # noqa: BLE001
        print(f"  2x2 对比: SKIP ({_safe_error(exc)})：读取同步层 Cookie 失败")
        return
    if not cookie:
        print("  2x2 对比: SKIP（同步层没有 LINUXDO 的 Cookie）")
        return
    if cffi_requests is None:
        print("  2x2 对比: SKIP（curl_cffi 未安装）")
        return

    parsed = parse_cookie(cookie)
    kept_cookie = cookie_string(parsed)
    dropped = {key: value for key, value in parsed.items() if key.lower() not in CF_COOKIES}
    dropped_cookie = cookie_string(dropped)
    print(f"  同步层含 cf_clearance={_yes('cf_clearance' in parsed)}（值不显示）")

    for imp in MATRIX_IMPERSONATES:
        for label, value, keep in (
            ("保留cf", kept_cookie, True),
            ("丢弃cf", dropped_cookie, False),
        ):
            headers = {"Cookie": value}
            # 与站点行为一致：只有保留 cf_clearance 时才注入面板 UA
            if ua and keep:
                headers["User-Agent"] = ua
            try:
                session = _new_session(
                    proxy_map, mode="ctor" if proxy_map else "direct", impersonate=imp
                )
                resp = session.get(LINUXDO_SESSION_URL, headers=headers, timeout=TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{imp} × {label}]: FAIL ({_safe_error(exc)})")
                continue
            h = resp.headers or {}
            body = (resp.text or "")[:4000].lower()
            print(
                f"  [{imp} × {label}]: HTTP {resp.status_code} "
                f"is_200={_yes(resp.status_code == 200)} "
                f"current_user={_yes('current_user' in body)} "
                f"cf-ray={_yes(bool(h.get('cf-ray')))} "
                f"cf-mitigated={_yes(bool(h.get('cf-mitigated')))} "
                f"server_cloudflare={_yes('cloudflare' in str(h.get('server') or '').lower())} "
                f"body_cf_marker={_yes(any(marker in body for marker in CF_MARKERS))}"
            )
    print(
        "  判读: 只有某一种指纹成功 → UA / TLS 家族不匹配；"
        "保留列全败而丢弃列成功 → cf_clearance 与出口/UA 绑定失效；"
        "两列全败 → 出口 IP / 节点信誉。"
    )


def main() -> int:
    args = _parse_args()
    proxy = _proxy_from_args(args)
    check_login = bool(getattr(args, "check_login", False))
    impersonate = str(getattr(args, "impersonate", "") or IMPERSONATE)
    ua = str(getattr(args, "ua", "") or "")
    _check_env()
    version = (
        str(getattr(cffi_requests, "__version__", "unknown"))
        if cffi_requests is not None
        else "NOT INSTALLED"
    )
    print(f"  curl_cffi: {version}（firefox153/154/155 需要 >=0.17）")
    print(f"  选中代理: {_describe_proxy(proxy) if proxy else '(none, 直连模式)'}")

    print("== TCP 连通性 ==")
    if proxy:
        _check_tcp(proxy)
    else:
        print("  TCP 连通: SKIP (未提供代理)")

    proxy_map = {"http": proxy, "https": proxy} if proxy else None
    if cffi_requests is None:
        print("== curl_cffi: MISSING，跳过三种模式对比 ==")
    else:
        print("== 出口 IP（curl_cffi 三种模式）==")
        _check_exit_ip(None, mode="direct")
        if proxy_map:
            _check_exit_ip(proxy_map, mode="ctor")
            _check_exit_ip(proxy_map, mode="post")
        else:
            print("  未提供代理：跳过 ctor-proxies / post-assign 两种模式。")

        print("== LinuxDo 接口特征（匿名）==")
        _check_linuxdo(proxy_map)

    # 独立于 curl_cffi 是否可用：缺依赖时 _check_login_state 内部也会打印 SKIP
    if check_login:
        print("== LinuxDo 登录态只读探测（--check-login）==")
        _check_login_state(proxy_map, impersonate=impersonate, ua=ua)
        if cffi_requests is not None:
            print("== LinuxDo 2x2 只读对比（指纹 × cf_clearance）==")
            _check_login_matrix(proxy_map, ua=ua)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
