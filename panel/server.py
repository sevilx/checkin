"""网页面板：只用标准库（http.server），不引入任何 Web 框架。

安全默认
--------
* 必须登录才能看到任何内容（状态里含账号昵称、余额）；
* 密码只存 PBKDF2-HMAC-SHA256 派生值 + 随机盐，文件权限 0600；
* 会话是 HMAC 签名的短票据，HttpOnly + SameSite=Lax，默认 7 天过期；
* 登录失败按 IP 限速，避免被爆破；
* 页面不加载任何外部资源（CDN、字体、统计脚本都没有），CSP 只允许 self。

跑起来：`python -m panel.server`（或 docker 里由 entrypoint 启动）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from checkin import schedule as sched
from checkin import settings as cfgstore
from checkin import store
from checkin.config import account_source, collect_accounts, load_file_config
from checkin.core import Site, mask, verify_account
from checkin.sites import BY_KEY, SITES

ROOT = Path(__file__).resolve().parent.parent
PANEL_DIR = Path(__file__).resolve().parent
AUTH_FILE = store.DATA_DIR / "panel_auth.json"

PANEL_HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))
PANEL_TITLE = os.environ.get("PANEL_TITLE", "checkin")
ALLOW_RUN = os.environ.get("PANEL_ALLOW_RUN", "true").lower() != "false"
ALLOW_EDIT = os.environ.get("PANEL_ALLOW_EDIT", "true").lower() != "false"
SESSION_DAYS = int(os.environ.get("PANEL_SESSION_DAYS", "7"))
# 套了 HTTPS 反代就打开它，会话 cookie 只走加密连接
SECURE_COOKIE = os.environ.get("PANEL_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on")
ITERATIONS = 120_000
COOKIE_NAME = "checkin_sid"
# 这些字段只以打码形式回传到浏览器，保存时留空表示"不修改"
SECRET_FIELDS = {"cookie", "a2", "password", "token", "sson"}
MAX_BODY = 256 * 1024

LOGIN_WINDOW = 300
LOGIN_LIMIT = 6
_failures: dict[str, list[float]] = {}
_fail_lock = threading.Lock()
_run_lock = threading.Lock()


# ---------------------------------------------------------------------- #
# 口令与会话
# ---------------------------------------------------------------------- #
def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS).hex()


def _load_auth() -> dict:
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("hash") and data.get("salt") and data.get("secret"):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_auth(payload: dict) -> None:
    store.ensure_dir()
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, AUTH_FILE)


def _make_auth(password: str, source: str, extra: dict | None = None) -> dict:
    salt = secrets.token_bytes(16)
    auth = {
        "salt": salt.hex(),
        "hash": _hash(password, salt),
        # 轮换签名密钥：改口令后所有旧会话立即失效
        "secret": secrets.token_hex(32),
        "source": source,
    }
    if extra:
        auth.update(extra)
    return auth


def _env_fingerprint(password: str) -> str:
    """记住 .env 里的 PANEL_PASSWORD 是哪一份，用来判断"环境变量有没有被改过"。"""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"panel-env", ITERATIONS).hex()


def init_auth() -> None:
    """确定面板口令。

    * 有 PANEL_PASSWORD：以它为准，但网页上改过的口令不会被覆盖，
      除非你改了 .env 里的 PANEL_PASSWORD（指纹变了才会重新同步）；
    * 没有 PANEL_PASSWORD：沿用已保存的口令，没有就生成一个打印到启动日志。
    """
    auth = _load_auth()
    env_password = os.environ.get("PANEL_PASSWORD", "").strip()

    if env_password:
        fingerprint = _env_fingerprint(env_password)
        if not auth or auth.get("env_fp") != fingerprint:
            _save_auth(_make_auth(env_password, "env", {"env_fp": fingerprint, "changed_via_panel": False}))
            print("[panel] 已按 PANEL_PASSWORD 更新面板口令，旧会话已失效")
            return
        if auth.get("changed_via_panel"):
            print("[panel] 面板口令已在网页上修改过，保留面板设置（改 .env 的 PANEL_PASSWORD 会覆盖它）")
            return
        print("[panel] 使用 PANEL_PASSWORD 指定的口令")
        return

    if auth:
        print("[panel] 使用已保存的面板口令（忘了就设置 PANEL_PASSWORD 后重启）")
        return

    generated = secrets.token_urlsafe(9)
    _save_auth(_make_auth(generated, "generated", {"changed_via_panel": False}))
    print("\n" + "=" * 58)
    print("  面板首次启动，已生成随机口令（只显示这一次）：")
    print(f"      {generated}")
    print("  想固定下来就设置环境变量 PANEL_PASSWORD=你的密码 后重启，")
    print("  或者登录后在「设置」页里直接改。")
    print("=" * 58 + "\n")


def password_source() -> str:
    """返回口令来源：env（来自 .env） / panel（网页改过） / generated（随机生成） / 未知。"""
    auth = _load_auth()
    if auth.get("changed_via_panel"):
        return "panel"
    return str(auth.get("source") or "")


def change_password(current: str, new: str) -> tuple[bool, str]:
    if not verify_password(current):
        return False, "当前口令不正确"
    if len(new) < 8:
        return False, "新口令至少 8 位"
    if new == current:
        return False, "新口令和当前口令一样"
    old = _load_auth()
    _save_auth(
        _make_auth(
            new,
            "panel",
            {"env_fp": old.get("env_fp", ""), "changed_via_panel": True},
        )
    )
    return True, "口令已更新，其他设备上的登录已失效"


def verify_password(password: str) -> bool:
    auth = _load_auth()
    if not auth:
        return False
    candidate = _hash(password, bytes.fromhex(auth["salt"]))
    return hmac.compare_digest(candidate, auth["hash"])


def issue_token() -> str:
    secret = bytes.fromhex(_load_auth().get("secret", "")) or b""
    expires = int(time.time()) + SESSION_DAYS * 86400
    signature = hmac.new(secret, str(expires).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def token_valid(token: str) -> bool:
    try:
        expires_text, signature = token.split(".", 1)
        expires = int(expires_text)
    except (ValueError, AttributeError):
        return False
    if expires < time.time():
        return False
    secret = bytes.fromhex(_load_auth().get("secret", ""))
    expected = hmac.new(secret, expires_text.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _rate_limited(ip: str) -> bool:
    now = time.time()
    with _fail_lock:
        hits = [t for t in _failures.get(ip, []) if now - t < LOGIN_WINDOW]
        _failures[ip] = hits
        return len(hits) >= LOGIN_LIMIT


def _record_failure(ip: str) -> None:
    with _fail_lock:
        _failures.setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------------- #
# 触发签到
# ---------------------------------------------------------------------- #
# 运行锁放在 store 里，和常驻调度器（checkin/scheduler.py）共用，
# 避免"面板点了立即签到"和"定时到点"同时跑两轮。
_acquire_lock = store.acquire_run_lock
_release_lock = store.release_run_lock


def trigger_run() -> tuple[bool, str]:
    with _run_lock:
        if not _acquire_lock():
            return False, "已有任务在运行，请稍后再试"
        store.set_run_state({"started_at": datetime.now().isoformat(timespec="seconds"), "source": "panel"})
        log_path = store.DATA_DIR / "last-run.log"
        env = os.environ.copy()
        env["CHECKIN_DATA_DIR"] = str(store.DATA_DIR)
        try:
            log = open(log_path, "wb")
            proc = subprocess.Popen(
                [sys.executable, "-u", "main.py"],
                cwd=str(ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            _release_lock()
            store.clear_run_state()
            return False, f"启动失败: {exc}"

        def _wait() -> None:
            try:
                proc.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                proc.kill()
            finally:
                log.close()
                _release_lock()
                store.clear_run_state()

        threading.Thread(target=_wait, daemon=True).start()
        return True, "已开始执行，稍后刷新查看结果"


# ---------------------------------------------------------------------- #
# 设置页：读 / 写 / 检测
# ---------------------------------------------------------------------- #
def _editable_fields(site_cls) -> list[str]:
    """proxy / name 单独有输入框，不混在字段列表里重复展示。"""
    return [f for f in site_cls.fields if f not in ("proxy", "name")]


def _account_view(site_cls, account: dict, index: int) -> dict:
    values: dict[str, str] = {}
    has: dict[str, bool] = {}
    for field in _editable_fields(site_cls):
        value = str(account.get(field) or "")
        has[field] = bool(value)
        values[field] = mask(value) if (field in SECRET_FIELDS and value) else value
    return {
        "index": index,
        "name": str(account.get("name") or account.get("remark") or ""),
        "values": values,
        "has": has,
        "proxy": str(account.get("proxy") or ""),
    }


def schedule_payload() -> dict:
    """给前端和 /api/status 用的时间表快照。"""
    cfg = sched.load()
    now = datetime.now()
    nxt = sched.next_run(cfg, now) if cfg["enabled"] else None
    return {
        "enabled": cfg["enabled"],
        "times": cfg["times"],
        "source": cfg["source"],
        "env_run_at": os.environ.get("RUN_AT", ""),
        "next_run": nxt.isoformat(timespec="minutes") if nxt else None,
        "now": now.isoformat(timespec="minutes"),
        "timezone": time.strftime("%Z"),
    }


def build_settings_payload() -> dict:
    file_cfg = load_file_config()
    sites = []
    for site_cls in SITES:
        accounts = collect_accounts(site_cls, file_cfg)
        sites.append(
            {
                "key": site_cls.key,
                "name": site_cls.name,
                "doc": site_cls.doc,
                "fields": _editable_fields(site_cls),
                "required": list(site_cls.required),
                "secret_fields": [f for f in _editable_fields(site_cls) if f in SECRET_FIELDS],
                "source": account_source(site_cls, file_cfg),
                "supports_check": site_cls.verify is not Site.verify,
                "accounts": [_account_view(site_cls, account, i) for i, account in enumerate(accounts)],
            }
        )

    site_proxies = {}
    for site_cls in SITES:
        site_proxies[site_cls.key] = cfgstore.site_proxy(site_cls.key) or os.environ.get(
            f"{site_cls.key}_PROXY", ""
        )

    return {
        "title": PANEL_TITLE,
        "allow_edit": ALLOW_EDIT,
        "allow_run": ALLOW_RUN,
        "sites": sites,
        "proxy": {
            "global": cfgstore.global_proxy(),
            "global_env": os.environ.get("GLOBAL_PROXY", ""),
            "sites": site_proxies,
        },
        "password": {"source": password_source()},
        "schedule": schedule_payload(),
    }


def save_site_accounts(key: str, incoming: list) -> tuple[bool, str]:
    site_cls = BY_KEY.get(str(key).lower())
    if not site_cls:
        return False, "未知站点"

    current = collect_accounts(site_cls, load_file_config())
    merged: list[dict] = []
    for item in incoming or []:
        if not isinstance(item, dict):
            continue
        origin = item.get("from")
        base = current[origin] if isinstance(origin, int) and 0 <= origin < len(current) else {}
        account: dict = {}

        name = str(item.get("name") or "").strip()
        if name:
            account["name"] = name

        snapshot = item.get("values") or {}
        for field in _editable_fields(site_cls):
            if field not in snapshot:
                continue
            raw = snapshot.get(field)
            value = "" if raw is None else str(raw).strip()
            if value:
                account[field] = value
            elif field in SECRET_FIELDS and base.get(field):
                # 密文字段：前端拿到的是打码值，留空表示"保持原样"
                account[field] = str(base[field])

        proxy = str(item.get("proxy") or "").strip()
        if proxy:
            account["proxy"] = proxy

        if not account:
            continue
        missing = [f for f in site_cls.required if not account.get(f)]
        if missing:
            return False, f"{site_cls.name} 缺少必填字段：{'、'.join(missing)}"
        if site_cls.key == "V2EX" and not (account.get("cookie") or account.get("a2")):
            return False, "V2EX 需要 cookie 或 a2 之一"
        merged.append(account)

    cfgstore.set_site(site_cls.key, merged)
    if merged:
        return True, f"{site_cls.name} 已保存 {len(merged)} 个账号"
    return True, f"{site_cls.name} 已清空，改用 .env / config.json 的配置"


def check_account(key: str, index) -> tuple[str, str]:
    site_cls = BY_KEY.get(str(key).lower())
    if not site_cls:
        return "unknown", "未知站点"
    accounts = collect_accounts(site_cls, load_file_config())
    if not (0 <= index < len(accounts)):
        return "unknown", "这个账号不存在，先保存配置再检测"
    return verify_account(site_cls, accounts[index], index)


# ---------------------------------------------------------------------- #
# HTTP 处理
# ---------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "checkin"
    sys_version = ""

    # -- 基础工具 -------------------------------------------------------- #
    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200, extra: dict | None = None) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _html(self, text: str, code: int = 200, extra: dict | None = None) -> None:
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8", extra)

    def _token(self) -> str:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:  # noqa: BLE001
            return ""
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    def _authed(self) -> bool:
        return token_valid(self._token())

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def log_message(self, fmt: str, *args) -> None:  # 不打请求日志，避免把路径/参数写进容器日志
        return

    def _same_origin(self) -> bool:
        """带 Origin 的请求必须是同源，挡住跨站伪造的 POST。"""
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        return urlparse(origin).netloc == self.headers.get("Host", "")

    def _json_body(self, raw: bytes) -> dict:
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # -- 路由 ------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return

        if path == "/login":
            if self._authed():
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._html(self._login_page())
            return

        if path == "/" or path == "/index.html":
            if not self._authed():
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self._html(self._page())
            return

        if path == "/settings":
            if not self._authed():
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self._html(self._render("settings.html"))
            return

        if path.startswith("/api/"):
            if not self._authed():
                self._json({"error": "unauthorized"}, 401)
                return
            if path == "/api/status":
                payload = store.load_status()
                payload["running"] = bool(store.get_run_state())
                payload["runs"] = store.load_runs()[:10]
                payload["schedule"] = schedule_payload()
                self._json(payload)
                return
            if path == "/api/settings":
                self._json(build_settings_payload())
                return
            self._json({"error": "not found"}, 404)
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if 0 < length <= MAX_BODY else b""

        if path == "/login":
            ip = self._client_ip()
            if _rate_limited(ip):
                self._html(self._login_page("尝试次数过多，请 5 分钟后再试"), 429)
                return
            form = parse_qs(raw.decode("utf-8", "ignore"))
            password = (form.get("password") or [""])[0]
            if not verify_password(password):
                _record_failure(ip)
                self._html(self._login_page("口令不正确"), 401)
                return
            cookie = (
                f"{COOKIE_NAME}={issue_token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"
                + ("; Secure" if SECURE_COOKIE else "")
            )
            self._html(
                "<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='0;url=/'>",
                302,
                {"Location": "/", "Set-Cookie": cookie},
            )
            return

        if path == "/logout":
            self._html(
                "<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='0;url=/login'>",
                302,
                {"Location": "/login", "Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"},
            )
            return

        if path == "/api/run":
            if not self._authed():
                self._json({"error": "unauthorized"}, 401)
                return
            if not self._same_origin():
                self._json({"error": "forbidden"}, 403)
                return
            if not ALLOW_RUN:
                self._json({"error": "面板已禁用“立即签到”"}, 403)
                return
            ok, message = trigger_run()
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
            return

        if path.startswith("/api/"):
            if not self._authed():
                self._json({"error": "unauthorized"}, 401)
                return
            if not self._same_origin():
                self._json({"error": "forbidden"}, 403)
                return
            body = self._json_body(raw)

            if path == "/api/settings":
                if not ALLOW_EDIT:
                    self._json({"ok": False, "message": "面板已禁用配置修改（PANEL_ALLOW_EDIT=false）"}, 403)
                    return
                sites = body.get("sites")
                if isinstance(sites, dict):
                    for key, accounts in sites.items():
                        if not isinstance(accounts, list):
                            continue
                        ok, message = save_site_accounts(key, accounts)
                        if not ok:
                            self._json({"ok": False, "message": message}, 400)
                            return
                proxies = body.get("proxy")
                if isinstance(proxies, dict):
                    global_value = proxies.get("global")
                    site_values = proxies.get("sites")
                    cfgstore.set_proxies(
                        None if global_value is None else str(global_value),
                        site_values if isinstance(site_values, dict) else None,
                    )
                self._json({"ok": True, "message": "已保存，下次签到生效", "settings": build_settings_payload()})
                return

            if path == "/api/password":
                if not ALLOW_EDIT:
                    self._json({"ok": False, "message": "面板已禁用配置修改（PANEL_ALLOW_EDIT=false）"}, 403)
                    return
                ok, message = change_password(str(body.get("current") or ""), str(body.get("next") or ""))
                if not ok:
                    self._json({"ok": False, "message": message}, 400)
                    return
                cookie = (
                    f"{COOKIE_NAME}={issue_token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"
                    + ("; Secure" if SECURE_COOKIE else "")
                )
                self._json({"ok": True, "message": message, "source": password_source()}, 200, {"Set-Cookie": cookie})
                return

            if path == "/api/check":
                key = str(body.get("site") or "")
                try:
                    index = int(body.get("index"))
                except (TypeError, ValueError):
                    index = -1
                status, message = check_account(key, index)
                self._json({"status": status, "message": message})
                return

            if path == "/api/schedule":
                if not ALLOW_EDIT:
                    self._json({"ok": False, "message": "面板已禁用配置修改（PANEL_ALLOW_EDIT=false）"}, 403)
                    return
                enabled = bool(body.get("enabled"))
                times, bad = sched.parse(body.get("times"))
                if bad:
                    self._json(
                        {"ok": False, "message": f"这些时间点格式不对：{'、'.join(bad)}，请用 HH:MM，如 09:10"},
                        400,
                    )
                    return
                if enabled and not times:
                    self._json({"ok": False, "message": "启用自动运行时至少要填一个时间点"}, 400)
                    return
                saved = sched.save(enabled, times)
                if saved["enabled"] and not saved["times"]:
                    self._json({"ok": False, "message": "时间点不能为空"}, 400)
                    return
                message = (
                    f"已保存，每天 {'、'.join(saved['times'])} 自动签到（15 秒内生效，无需重启）"
                    if saved["enabled"]
                    else "已停用自动运行，只能手动点“立即签到”"
                )
                self._json({"ok": True, "message": message, "schedule": schedule_payload()})
                return

            self._json({"error": "not found"}, 404)
            return

        self._json({"error": "not found"}, 404)

    # -- 页面 ------------------------------------------------------------ #
    def _render(self, name: str) -> str:
        return (PANEL_DIR / name).read_text(encoding="utf-8").replace("{{TITLE}}", PANEL_TITLE)

    def _page(self) -> str:
        return self._render("index.html")

    def _login_page(self, error: str = "") -> str:
        template = (PANEL_DIR / "login.html").read_text(encoding="utf-8")
        notice = f"<p class='error'>{error}</p>" if error else ""
        hint = ""
        if password_source() == "generated":
            hint = (
                "<p class='error' style='color:var(--muted)'>"
                "首次启动的随机口令只打印在容器日志里："
                "<code>docker logs checkin | grep 面板口令</code>"
                "</p>"
            )
        return (
            template.replace("{{TITLE}}", PANEL_TITLE)
            .replace("{{HINT}}", hint)
            .replace("{{ERROR}}", notice)
        )


def main() -> None:
    init_auth()
    store.ensure_dir()
    server = ThreadingHTTPServer((PANEL_HOST, PANEL_PORT), Handler)
    print(f"[panel] 监听 http://{PANEL_HOST}:{PANEL_PORT}  标题={PANEL_TITLE}  允许手动签到={ALLOW_RUN}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[panel] 已停止")


if __name__ == "__main__":
    main()
