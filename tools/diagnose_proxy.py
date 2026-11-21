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

脚本只读：不写文件、不改配置、不携带任何登录态（不构造 Cookie 头）。
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
    from checkin.core import _HIGH_SPECIFIC_BLOCK_MARKERS as CF_MARKERS
except Exception:  # noqa: BLE001
    CF_MARKERS = (
        "cf-error-details",
        "attention required",
        "just a moment",
        "enable javascript and cookies",
        "cf_chl_opt",
        "cf_clearance",
    )

IP_URL = "https://api.ipify.org?format=json"
LINUXDO_SESSION_URL = "https://linux.do/session/current.json"
IMPERSONATE = "chrome120"
TIMEOUT = 12


def _yes(value: bool) -> str:
    return "YES" if value else "NO"


def _safe_error(exc: BaseException) -> str:
    """异常文本可能带完整代理 URL（含口令），只回异常类名。"""
    return type(exc).__name__


def _proxy_from_args() -> str:
    parser = argparse.ArgumentParser(
        description="只读诊断代理连通性、出口 IP 与 LinuxDo 404 性质（不打印凭据 / 正文）。"
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="代理地址，如 <scheme>://<host>:<port>（请写出端口）；缺省时读 DIAG_PROXY / GLOBAL_PROXY 环境变量",
    )
    args = parser.parse_args()
    raw = str(args.proxy or os.environ.get("DIAG_PROXY") or os.environ.get("GLOBAL_PROXY") or "")
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


def _new_session(proxy_map: dict[str, str] | None, mode: str):
    """mode: direct / ctor / post；与 core.session() 的三种 curl_cffi 模式对齐。"""
    if cffi_requests is None:
        raise RuntimeError("curl_cffi missing")
    if proxy_map and mode == "ctor":
        try:
            return cffi_requests.Session(impersonate=IMPERSONATE, proxies=proxy_map)
        except TypeError:  # 老版本不认构造参数，退回属性赋值
            sess = cffi_requests.Session()
            sess.impersonate = IMPERSONATE
            sess.proxies = dict(proxy_map)
            return sess
    sess = cffi_requests.Session(impersonate=IMPERSONATE)
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


def main() -> int:
    proxy = _proxy_from_args()
    _check_env()
    print(f"  选中代理: {_describe_proxy(proxy) if proxy else '(none, 直连模式)'}")

    print("== TCP 连通性 ==")
    if proxy:
        _check_tcp(proxy)
    else:
        print("  TCP 连通: SKIP (未提供代理)")

    if cffi_requests is None:
        print("== curl_cffi: MISSING，跳过三种模式对比 ==")
        return 0

    proxy_map = {"http": proxy, "https": proxy} if proxy else None
    print("== 出口 IP（curl_cffi 三种模式）==")
    _check_exit_ip(None, mode="direct")
    if proxy_map:
        _check_exit_ip(proxy_map, mode="ctor")
        _check_exit_ip(proxy_map, mode="post")
    else:
        print("  未提供代理：跳过 ctor-proxies / post-assign 两种模式。")

    print("== LinuxDo 接口特征 ==")
    _check_linuxdo(proxy_map)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
