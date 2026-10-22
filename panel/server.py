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

from checkin import store

ROOT = Path(__file__).resolve().parent.parent
PANEL_DIR = Path(__file__).resolve().parent
AUTH_FILE = store.DATA_DIR / "panel_auth.json"

PANEL_HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))
PANEL_TITLE = os.environ.get("PANEL_TITLE", "checkin")
ALLOW_RUN = os.environ.get("PANEL_ALLOW_RUN", "true").lower() != "false"
SESSION_DAYS = int(os.environ.get("PANEL_SESSION_DAYS", "7"))
# 套了 HTTPS 反代就打开它，会话 cookie 只走加密连接
SECURE_COOKIE = os.environ.get("PANEL_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on")
ITERATIONS = 120_000
COOKIE_NAME = "checkin_sid"

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


def init_auth() -> None:
    """确定面板口令：优先用 PANEL_PASSWORD，没有就生成一个并打印到启动日志。"""
    auth = _load_auth()
    env_password = os.environ.get("PANEL_PASSWORD", "").strip()

    if env_password:
        if not auth or not hmac.compare_digest(_hash(env_password, bytes.fromhex(auth["salt"])), auth["hash"]):
            salt = secrets.token_bytes(16)
            auth = {
                "salt": salt.hex(),
                "hash": _hash(env_password, salt),
                # 改口令同时轮换签名密钥，旧会话立即全部失效
                "secret": secrets.token_hex(32),
                "source": "env",
            }
            _save_auth(auth)
            print("[panel] 已按 PANEL_PASSWORD 更新面板口令，旧会话已失效")
        return

    if auth:
        print("[panel] 使用已保存的面板口令（如需重置，设置 PANEL_PASSWORD 后重启）")
        return

    generated = secrets.token_urlsafe(9)
    salt = secrets.token_bytes(16)
    _save_auth(
        {
            "salt": salt.hex(),
            "hash": _hash(generated, salt),
            "secret": secrets.token_hex(32),
            "source": "generated",
        }
    )
    print("\n" + "=" * 58)
    print("  面板首次启动，已生成随机口令（只显示这一次）：")
    print(f"      {generated}")
    print("  想固定下来就设置环境变量 PANEL_PASSWORD=你的密码 后重启")
    print("=" * 58 + "\n")


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
def _acquire_lock() -> bool:
    store.ensure_dir()
    path = store.DATA_DIR / ".run.lock"
    try:
        if path.exists() and time.time() - path.stat().st_mtime > 1800:
            path.unlink()
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def _release_lock() -> None:
    try:
        (store.DATA_DIR / ".run.lock").unlink()
    except OSError:
        pass


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

        if path.startswith("/api/"):
            if not self._authed():
                self._json({"error": "unauthorized"}, 401)
                return
            if path == "/api/status":
                payload = store.load_status()
                payload["running"] = bool(store.get_run_state())
                payload["runs"] = store.load_runs()[:10]
                self._json(payload)
                return
            self._json({"error": "not found"}, 404)
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if 0 < length <= 8192 else b""

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
            if not ALLOW_RUN:
                self._json({"error": "面板已禁用“立即签到”"}, 403)
                return
            ok, message = trigger_run()
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
            return

        self._json({"error": "not found"}, 404)

    # -- 页面 ------------------------------------------------------------ #
    def _page(self) -> str:
        return (PANEL_DIR / "index.html").read_text(encoding="utf-8").replace("{{TITLE}}", PANEL_TITLE)

    def _login_page(self, error: str = "") -> str:
        template = (PANEL_DIR / "login.html").read_text(encoding="utf-8")
        notice = f"<p class='error'>{error}</p>" if error else ""
        return template.replace("{{TITLE}}", PANEL_TITLE).replace("{{ERROR}}", notice)


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
