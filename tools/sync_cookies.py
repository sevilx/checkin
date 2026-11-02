#!/usr/bin/env python3
"""把本机浏览器里的登录态，一键推送到 NAS 上的签到面板。

为什么是这个思路
----------------
论坛（尤其 52pojie 的瑞数、LinuxDo 的 Cloudflare）的会话 cookie 只有几小时到
几天有效期。你每天本来就会用浏览器逛这些站，cookie 一直是最新的；与其在 NAS 上
装个 1GB 的 Playwright 去"模拟登录"，不如把浏览器里现成的那份同步过去。

零第三方依赖（只用标准库），因为这是给 Windows 本机双击运行的脚本，
不该为了传几个 cookie 先装一堆包。

用法
----
    # 最常用：自动找 Firefox，推到 NAS
    python tools/sync_cookies.py --nas http://192.168.1.10:8080

    # 第一次会问面板口令，之后用 Windows DPAPI 记住（只有你这台机器的这个账户能解）
    python tools/sync_cookies.py --nas http://192.168.1.10:8080 --remember

    # 只看会推送什么，不真的发出去
    python tools/sync_cookies.py --dry-run

    # 只同步两个站
    python tools/sync_cookies.py --nas ... --sites pojie,kafan,linuxdo

    # 顺手让 NAS 立刻验一次登录态
    python tools/sync_cookies.py --nas ... --check

    # Edge / Chrome 用户：先用 Cookie-Editor 扩展导出 JSON，再喂进来
    python tools/sync_cookies.py --nas ... --from-json cookies.json

    # 看看本机有哪些浏览器 profile
    python tools/sync_cookies.py --list

读到的是 cookie 数据库副本，不会改动浏览器本身；Firefox 正在运行也能读。
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------- #
# 站点 <-> 域名
# ---------------------------------------------------------------------- #
# 只要 host 等于域名或它的子域，就算这个站点的 cookie。
# 百度系两个站点共用 .baidu.com 下的 BDUSS，所以都配 "baidu.com"，
# 拿到同一份登录态是对的（多带几个无关 cookie 不影响）。
DOMAINS: dict[str, tuple[str, ...]] = {
    "BAIDUWP": ("baidu.com",),
    "TIEBA": ("baidu.com",),
    "BILIBILI": ("bilibili.com",),
    "SMZDM": ("smzdm.com",),
    "V2EX": ("v2ex.com",),
    "NODESEEK": ("nodeseek.com",),
    "LINUXDO": ("linux.do",),
    "FNNASCLUB": ("fnnas.com",),
    "WNFLB": ("wnflb2023.com",),
    "POJIE": ("52pojie.cn",),
    "KAFAN": ("kafan.cn",),
}

# 这些站点的 cookie 拿过去也没用：天翼云盘走账密 + SDK，跟浏览器会话无关
SKIP_HINT = {"TY189": "账密登录站点，不需要同步 cookie"}

STORE_NAME = "checkin-sync"


# ---------------------------------------------------------------------- #
# 本机小配置（记住 profile 路径、记住口令）
# ---------------------------------------------------------------------- #
def store_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / STORE_NAME


def config_file() -> Path:
    return store_dir() / "config.json"


def load_config() -> dict:
    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(**kwargs) -> None:
    data = load_config()
    data.update({key: value for key, value in kwargs.items() if value})
    try:
        path = config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------- #
# 输出
# ---------------------------------------------------------------------- #
def _say(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:  # 老 cmd 用 GBK，实在打不出来就降级
        print(text.encode("gbk", "replace").decode("gbk"))


def _ok(text: str) -> None:
    _say(f"  [OK]   {text}")


def _warn(text: str) -> None:
    _say(f"  [!!]   {text}")


def _info(text: str) -> None:
    _say(f"         {text}")


# ---------------------------------------------------------------------- #
# Firefox profile 发现
# ---------------------------------------------------------------------- #
def firefox_root() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Mozilla" / "Firefox"
    return None


def parse_profiles_ini(root: Path) -> list[dict]:
    """极简 INI 解析：只要 [ProfileN] 段里的 Path / IsRelative / Default。"""
    ini = root / "profiles.ini"
    if not ini.exists():
        return []
    profiles: list[dict] = []
    current: dict | None = None
    try:
        lines = ini.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if current is not None:
                profiles.append(current)
            section = line[1:-1]
            current = {"section": section} if section.lower().startswith("profile") else None
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    if current is not None:
        profiles.append(current)

    out: list[dict] = []
    for item in profiles:
        raw_path = item.get("Path")
        if not raw_path:
            continue
        relative = str(item.get("IsRelative", "1")) != "0"
        folder = (root / raw_path) if relative else Path(raw_path)
        out.append(
            {
                "path": folder,
                "name": item.get("Name") or folder.name,
                "default": str(item.get("Default", "0")) == "1",
            }
        )
    return out


def discover_firefox(explicit: str | None = None) -> list[dict]:
    """按可信度排好序返回候选 profile（每个都带 cookies.sqlite 是否存在）。

    没显式指定时，先看上次记住的路径 —— RunningCheese 这类便携版
    （Firefox 装在 D 盘、profile 直接放在 Profiles 目录下）不会被 profiles.ini 收录，
    每次手打一长串带空格的路径太不现实。
    """
    candidates: list[dict] = []

    if not explicit:
        remembered = str(load_config().get("profile") or "")
        if remembered and Path(remembered).exists():
            explicit = remembered

    if explicit:
        target = Path(explicit).expanduser()
        if target.is_file():
            candidates.append({"path": target.parent, "name": target.parent.name, "db": target, "default": True})
        elif target.is_dir():
            candidates.append({"path": target, "name": target.name, "db": target / "cookies.sqlite", "default": True})
        return [c for c in candidates if Path(c["db"]).exists()]

    root = firefox_root()
    if not root:
        return []

    for profile in parse_profiles_ini(root):
        db = profile["path"] / "cookies.sqlite"
        if db.exists():
            candidates.append({"path": profile["path"], "name": profile["name"], "db": db, "default": profile["default"]})

    # profiles.ini 不存在 / 被改坏时的兜底：直接扫目录
    profiles_dir = root / "Profiles"
    if profiles_dir.exists():
        for folder in sorted(profiles_dir.iterdir()):
            db = folder / "cookies.sqlite"
            if folder.is_dir() and db.exists() and not any(c["db"] == db for c in candidates):
                candidates.append({"path": folder, "name": folder.name, "db": db, "default": False})

    def rank(item):
        name = item["name"].lower()
        return (0 if item["default"] else 1, 0 if "default-release" in name else 1, -item["db"].stat().st_mtime)

    return sorted(candidates, key=rank)


# ---------------------------------------------------------------------- #
# 读 cookie
# ---------------------------------------------------------------------- #
def copy_db(path: Path) -> Path:
    """复制一份再读：Firefox 正在运行时数据库是锁着的，直接连会 database is locked。

    WAL 模式还要把 -wal 一起复制，否则最近写入的 cookie 可能还没落进主库。
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="checkin-cookies-"))
    target = tmpdir / "cookies.sqlite"
    shutil.copy2(path, target)
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            try:
                shutil.copy2(side, Path(str(target) + suffix))
            except OSError:
                pass
    return target


def rows_from_firefox(db: Path) -> list[dict]:
    tmp = copy_db(db)
    try:
        # 读的是副本，用普通模式最省事；URI 模式在 Windows 盘符路径上容易踩坑
        conn = sqlite3.connect(str(tmp))
        try:
            cursor = conn.execute(
                "SELECT host, name, value, expiry, lastAccessed "
                "FROM moz_cookies WHERE value <> '' ORDER BY lastAccessed DESC"
            )
            return [
                {
                    "domain": str(host),
                    "name": str(name),
                    "value": str(value),
                    "expiry": normalise_expiry(int(expiry or 0)),
                }
                for host, name, value, expiry, _ in cursor.fetchall()
            ]
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def rows_from_json(path: Path) -> list[dict]:
    """Cookie-Editor / EditThisCookie 导出的 JSON，数组或 {cookies:[...]} 都认。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cookies") or data.get("data") or []
    if not isinstance(data, list):
        raise ValueError("这个 JSON 不是 cookie 数组")

    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if not name or value in (None, ""):
            continue
        expiry = item.get("expirationDate") or item.get("expires") or 0
        try:
            expiry = int(float(expiry))
        except (TypeError, ValueError):
            expiry = 0
        out.append(
            {
                "domain": str(item.get("domain") or item.get("host") or ""),
                "name": str(name),
                "value": str(value),
                "expiry": normalise_expiry(expiry),
            }
        )
    return out


def host_matches(host: str, domain: str) -> bool:
    host = host.lstrip(".").lower()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def normalise_expiry(value: int) -> int:
    """把 expiry 统一成「秒」。

    Firefox 自己的库里存的是秒，但从 Chrome / Edge 导入过来的 cookie 存的是**毫秒**
    （实测一份 RunningCheese 的库里，52pojie 的 cookie 过期时间算出来是 57552 年后）。
    17 位量级（> 1e11）只可能是毫秒，直接除以 1000。
    """
    if not value or value <= 0:
        return 0
    if value > 100_000_000_000:
        return value // 1000
    return value


def iso_time(stamp: int) -> str | None:
    """把 unix 时间戳转成 ISO 字符串。

    真实 cookie 库里什么都有：毫秒时间戳、被站点设成 9999 年的极值，
    直接扔给 time.localtime() 会抛 OSError 22。这里统一兜住，
    超出可表示范围就当作"没有明确过期时间"。
    """
    stamp = normalise_expiry(stamp)
    if not stamp or stamp <= 0:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stamp))
    except (OSError, OverflowError, ValueError):
        return None


def build_cookies(rows: list[dict], wanted: set[str] | None = None) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """按站点聚合 cookie，返回 ({KEY: {cookie, expires}}, {KEY: [被丢掉的过期名]})。"""
    now = int(time.time())
    buckets: dict[str, dict[str, str]] = {}
    expires_at: dict[str, int] = {}
    dropped: dict[str, list[str]] = {}

    for row in rows:
        if row["expiry"] and row["expiry"] < now:
            continue  # 已经过期的，带上只会让服务端判定会话异常
        for key, domains in DOMAINS.items():
            if wanted and key not in wanted:
                continue
            if not any(host_matches(row["domain"], d) for d in domains):
                continue
            bucket = buckets.setdefault(key, {})
            if row["name"] in bucket:
                continue  # 同一个名字多条（容器标签页），保留最新那条
            bucket[row["name"]] = row["value"]
            if row["expiry"]:
                current = expires_at.get(key, 0)
                # 取最早过期的那个：整体会话通常由最脆弱的那条 cookie 决定
                if current == 0 or row["expiry"] < current:
                    expires_at[key] = row["expiry"]

    payload: dict[str, dict] = {}
    for key, cookies in buckets.items():
        if not cookies:
            continue
        text = "; ".join(f"{name}={value}" for name, value in cookies.items())
        payload[key] = {"cookie": text, "expires": iso_time(expires_at.get(key, 0))}
    return payload, dropped


# ---------------------------------------------------------------------- #
# 推送
# ---------------------------------------------------------------------- #
def secret_file() -> Path:
    return store_dir() / "panel_secret.bin"


def dpapi(data: bytes, protect: bool) -> bytes:
    """Windows 数据保护 API：只有当前用户的当前账户能解开，别的机器/别的用户无效。"""
    if os.name != "nt":
        raise OSError("DPAPI 只在 Windows 上可用")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    # 加 CRYPTPROTECT_UI_FORBIDDEN，避免弹窗要确认
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        raise OSError("DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def load_saved_password() -> str:
    env = os.environ.get("PANEL_PASSWORD", "").strip()
    if env:
        return env
    path = secret_file()
    if not path.exists():
        return ""
    try:
        return dpapi(path.read_bytes(), protect=False).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


def save_password(password: str) -> bool:
    try:
        path = secret_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(dpapi(password.encode("utf-8"), protect=True))
        return True
    except Exception:  # noqa: BLE001
        return False


def normalise_nas(value: str) -> str:
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    if not text.endswith("/api/cookies"):
        text = text + "/api/cookies"
    return text


def push(url: str, password: str, cookies: dict, check: bool, timeout: int = 20) -> dict:
    body = json.dumps(
        {"cookies": cookies, "source": "firefox-sync", "check": check},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {password}",
            "User-Agent": "checkin-cookie-sync/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# ---------------------------------------------------------------------- #
# 主流程
# ---------------------------------------------------------------------- #
def list_profiles() -> int:
    remembered = str(load_config().get("profile") or "")
    if remembered:
        _say(f"已记住的 profile：{remembered}")
        _say("（不加 --profile 时会自动用它；想换一个就再传一次 --profile）")
        _say("")
    profiles = discover_firefox()
    if not profiles:
        _warn("没找到 Firefox profile")
        _info(f"常见位置：{firefox_root()}")
        _info("如果 Firefox 装在别的盘 / 是便携版，用 --profile <目录> 手动指定")
        return 1
    _say(f"发现 {len(profiles)} 个 Firefox profile：")
    for index, item in enumerate(profiles):
        mark = " <- 默认" if item["default"] else ""
        size = item["db"].stat().st_size
        _say(f"  [{index}] {item['name']}{mark}")
        _info(f"{item['db']}  ({size // 1024} KB)")
    _info("用 --profile \"路径\" 指定，指定过一次就会记住")
    return 0


def describe(payload: dict) -> None:
    for key in sorted(payload):
        cookie = payload[key]["cookie"]
        count = len([part for part in cookie.split(";") if "=" in part])
        size = len(cookie)
        expire = payload[key].get("expires")
        tail = f"，最早 {expire} 过期" if expire else "，会话级 cookie"
        _say(f"  {key:<10} {count} 个字段 {size} 字节{tail}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="把本机浏览器 cookie 推送到 NAS 签到面板",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--nas", help="NAS 面板地址，如 http://192.168.1.10:8080")
    parser.add_argument("--password", help="面板口令（不填则读环境变量 / 已保存的 / 交互输入）")
    parser.add_argument("--remember", action="store_true", help="用 Windows DPAPI 记住口令，之后免输")
    parser.add_argument("--profile", help="Firefox profile 目录，或直接给 cookies.sqlite 路径")
    parser.add_argument("--from-json", help="改读 Cookie-Editor 等扩展导出的 JSON（Edge/Chrome 走这条）")
    parser.add_argument("--sites", help="只同步这些站点，逗号分隔，如 pojie,kafan")
    parser.add_argument("--dry-run", action="store_true", help="只显示会推什么，不发送")
    parser.add_argument("--check", action="store_true", help="推送后让 NAS 立刻验证登录态")
    parser.add_argument("--list", action="store_true", help="列出本机 Firefox profile")
    args = parser.parse_args(argv)

    if args.list:
        return list_profiles()

    # 1) 找 cookie 数据
    if args.from_json:
        path = Path(args.from_json).expanduser()
        if not path.exists():
            _warn(f"文件不存在：{path}")
            return 1
        rows = rows_from_json(path)
        source = f"JSON 导入（{path.name}）"
    else:
        profiles = discover_firefox(args.profile)
        if not profiles:
            _warn("没找到 Firefox 的 cookies.sqlite")
            _info("可能的原因：这台机器没装 / 没用过 Firefox，或者 profile 在别的盘")
            _info("排查：python tools/sync_cookies.py --list")
            _info("Edge / Chrome 用户：装 Cookie-Editor 扩展 → 导出 JSON → --from-json cookies.json")
            return 1
        picked = profiles[0]
        rows = rows_from_firefox(Path(picked["db"]))
        source = f"Firefox / {picked['name']}"
        # 便携版之类的自定义路径记下来，下次不用再传
        if args.profile and load_config().get("profile") != str(Path(picked["path"])):
            save_config(profile=str(Path(picked["path"])))
            _info(f"已记住 profile 路径，下次只写 --nas 就行")

    if not rows:
        _warn("cookie 库是空的（浏览器可能从没登录过这些站）")
        return 1

    # 2) 按站点归拢
    wanted = None
    if args.sites:
        wanted = {item.strip().upper() for item in args.sites.replace(" ", ",").split(",") if item.strip()}
        unknown = sorted(wanted - set(DOMAINS) - set(SKIP_HINT))
        if unknown:
            _warn(f"不认识的站点：{'、'.join(unknown)}")
            _info(f"可选：{'、'.join(sorted(DOMAINS))}")

    payload, _ = build_cookies(rows, wanted)

    _say("")
    _say(f"来源：{source}，共 {len(rows)} 条 cookie")
    if not payload:
        _warn("这些站点一个都没匹配上——你在这个浏览器里登录过它们吗？")
        _info("提示：cookie 是按域名归属的，必须用同一个浏览器访问过对应站点")
        return 1

    _say(f"匹配到 {len(payload)} 个站点：")
    describe(payload)
    missing = sorted(set(DOMAINS) - set(payload) - (wanted or set()))
    if missing:
        _say("")
        _info(f"这些站点没找到 cookie（跳过）：{'、'.join(missing)}")

    if args.dry_run:
        _say("")
        _ok("dry-run：以上是预览，没有发送任何数据")
        return 0

    # 3) 推送
    url = normalise_nas(args.nas or "")
    if not url:
        _warn("缺少 --nas 参数，例如 --nas http://192.168.1.10:8080")
        return 1

    password = (args.password or "").strip() or load_saved_password()
    if not password:
        try:
            password = getpass.getpass("面板口令（输入时不显示）：").strip()
        except (EOFError, KeyboardInterrupt):
            _warn("已取消")
            return 1
    if not password:
        _warn("口令不能为空")
        return 1

    _say("")
    _say(f"推送到 {url} ...")
    try:
        result = push(url, password, payload, check=args.check)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 401:
            _warn("口令不对（401）。检查 --password，或删除已保存的口令后重试")
            _info(str(secret_file()))
        elif exc.code == 403:
            _warn("面板禁止写入（403）。检查 PANEL_ALLOW_EDIT 是否被设成 false")
            _info(detail)
        else:
            _warn(f"服务器返回 HTTP {exc.code}")
            _info(detail)
        return 1
    except urllib.error.URLError as exc:
        _warn(f"连不上 NAS：{exc.reason}")
        _info("确认 1) NAS 的 IP 和端口 2) 容器在跑 3) 和 NAS 在同一局域网")
        return 1
    except Exception as exc:  # noqa: BLE001
        _warn(f"推送失败：{exc}")
        return 1

    if not result.get("ok"):
        _warn(str(result.get("message") or "面板没有接受这次推送"))
        return 1

    _ok(f"已写入 {result.get('written', 0)} 个站点（{result.get('updated_at', '')}）")
    ignored = result.get("ignored") or []
    if ignored:
        _info(f"面板忽略了这些（没有 cookie 字段的站点）：{'、'.join(ignored)}")

    checks = result.get("checks") or {}
    if checks:
        _say("")
        _say("登录态检测：")
        for key in sorted(checks):
            item = checks[key]
            status = item.get("status")
            label = {"ok": "有效", "bad": "已失效", "unknown": "没测出来", "unsupported": "不支持检测"}.get(status, status)
            _say(f"  {key:<10} {label}  {item.get('message', '')}")

    if args.remember:
        # 记的是"这次实际用的口令"，不管它是命令行传的、环境变量读的，还是刚交互输入的
        if save_password(password):
            _ok("口令已用 DPAPI 记住，下次不用再输")
        else:
            _warn("口令没能保存（DPAPI 不可用），下次仍需要输入")

    _say("")
    _say("完成。cookie 已进 data/cookies.json，下一次签到就会用它（不用重启容器）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
