"""HDWinG（hdwing.org）：NexusPHP 风格插件签到 + Cloudflare。

签到机制（按真实抓包证据实现，别套 Discuz 那套）：
* 登录票据是 ``hdchina``（不是 c_secure_* 那一套），站点还带 ``cf_clearance``；
* 签到按钮在首页 index.php：
  ``<a href="#" onclick="do_signin(this);"><b>签 到</b></a>``；
* do_signin 定义在 /plugin/js/base.js：先读 ``<meta name='x-csrf' content='...'>``，
  再 ``POST plugin_sign-in.php?cmd=signin``，form-urlencoded body 只有 ``csrf=<token>``；
* 返回裸 JSON：``state`` / ``signindays`` / ``integral``。
  signin.php / attendance.php / sign_in.php / sign.php 全是 404，别用这些路径。

因此 csrf **必须每次现取**，且取 csrf 和提交签到要用同一个会话（token 与会话绑定）。
Cloudflare 这一层沿用项目通用做法：默认丢掉和「出口 IP + UA」绑定的 cf_clearance，
靠 curl_cffi 指纹过被动检查；只有确认 NAS 与浏览器同出口时才 keep_cf=1 + ua 配对。
"""

from __future__ import annotations

import json
import re

from ..core import Line, NAV_HEADERS, Site, looks_blocked, parse_cookie

BASE = "https://hdwing.org"
HOME = f"{BASE}/index.php"
# 唯一正确的签到接口（free_signin.php / attendance.php 等都是 404）
SIGN_API = f"{BASE}/plugin_sign-in.php?cmd=signin"

# <meta name='x-csrf' content='...'>：单双引号、属性顺序都兼容
CSRF_META = re.compile(
    r"""<meta\b[^>]*?name\s*=\s*['"]x-csrf['"][^>]*?content\s*=\s*['"]([^'"]+)['"]"""
    r"""|<meta\b[^>]*?content\s*=\s*['"]([^'"]+)['"][^>]*?name\s*=\s*['"]x-csrf['"]""",
    re.I,
)
# 展示用的用户名（NexusPHP 首页的用户详情链接），取不到就算了，不强制
USER_LINK = re.compile(r"userdetails\.php\?id=\d+[^>]*>([^<]{1,40})</a>", re.I)

OK_STATE = "success"
# 除 success 外的 state 字符串站点没给全，按语义宽泛兜住「已签到」
DONE_HINTS = (
    "已签到", "已经签到", "今日已签", "今天已签", "重复签到", "请勿重复",
    "already", "signed", "repeat", "done",
)
LOGIN_HINTS = ("未登录", "请先登录", "请登录", "登录后", "需要登录", "not login", "please login", "login required")
CSRF_HINTS = ("csrf", "token", "invalid", "非法", "过期", "expired")


class HDWinG(Site):
    key = "HDWING"
    name = "HDWinG"
    fields = ("cookie", "ua", "keep_cf")
    doc = (
        "登录 hdwing.org → F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（登录票据是 hdchina，不是 c_secure_*）。站点挂在 Cloudflare 后面，"
        "默认丢掉和 IP+UA 绑定的 cf_clearance；只有 NAS 和浏览器同出口时才设 keep_cf=1，"
        "并把当时那个浏览器的 UA 填进 ua（三者必须一致，否则会更像伪造）"
    )
    # CF 挑战时依次尝试的指纹；没装 curl_cffi 时 core 会自动退回 requests 只跑一次
    cf_impersonates = ("safari15_5", "chrome120", "chrome110")

    def __init__(self, account: dict, index: int = 0):
        super().__init__(account, index)
        cookies = parse_cookie(self.cookie)
        if not cookies.get("hdchina"):
            raise ValueError(
                "Cookie 里没有 hdchina（HDWinG 的登录票据，不是 c_secure_uid 那套），"
                "请重新登录 hdwing.org 后从「F12 → 网络 → 请求标头 → Cookie」整行复制"
            )

    # ------------------------------------------------------------------ #
    # Cloudflare 排查提示：和「cookie 失效」明确分开，避免用户反复重登
    # ------------------------------------------------------------------ #
    def _cf_message(self) -> str:
        missing = (
            "\n当前环境没装 curl_cffi，只能用 requests 的默认 TLS 指纹，Cloudflare 一眼就能认出来；"
            "装上就多一层保障：pip install curl_cffi（Docker 镜像里已经预装）。"
            if self.cf_impersonates and not self._has_cffi()
            else ""
        )
        return (
            "HDWinG 被 Cloudflare 挑战了：请求在边缘就被拦下，根本没到站点服务器，"
            "这不是 cookie 失效，别反复重新登录。按这个顺序排查：\n"
            "1) 出口 IP 最要紧：机房 IP / 云服务器 / 部分机场节点会被直接挑战，"
            "换成家宽出口，或给这个站点单独配一条和浏览器同一个出口的代理；\n"
            "2) 默认已帮你丢掉浏览器里的 cf_clearance（它和签发时的 IP + UA 绑定，"
            "换台机器必然失效）；只有确认容器和浏览器同出口时，才设 keep_cf=1 "
            "并把当时那个浏览器的 UA 填进 ua，三者必须一致；\n"
            "3) 程序会自动换几种浏览器指纹重试，还是不行就用 tools/diagnose_proxy.py 看出口 IP。"
            f"{missing}"
        )

    @staticmethod
    def _has_cffi() -> bool:
        from ..core import has_curl_cffi
        return has_curl_cffi()

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_csrf(html: str) -> str:
        for match in CSRF_META.finditer(html or ""):
            token = (match.group(1) or match.group(2) or "").strip()
            if token:
                return token
        return ""

    @staticmethod
    def _who(html: str) -> str:
        match = USER_LINK.search(html or "")
        if not match:
            return ""
        name = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        if name and name not in ("登录", "注册", "退出"):
            return name
        return ""

    @staticmethod
    def _is_login_page(html: str) -> bool:
        lowered = (html or "").lower()
        if "login.php" in lowered:
            return True
        has_form = any(
            token in lowered
            for token in ('name="username"', "name='username'", 'name="password"', "name='password'")
        )
        # 有登录表单、又没有签到按钮/CSRF meta，才判定为登录页
        has_sign = "do_signin" in lowered or "x-csrf" in lowered
        return has_form and not has_sign

    @staticmethod
    def _json(text: str) -> dict | None:
        raw = (text or "").strip()
        if not raw:
            return None
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _plain(html: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()

    def _open_home(self):
        """拿首页 HTML（顺带现取 csrf）和已过 CF 的会话；两者必须一起用。"""
        cookie_str, kept_cf = self.cf_cookies()
        headers = self.cf_headers({**NAV_HEADERS, "Referer": f"{BASE}/"}, kept_cf)
        resp, session = self.browser_request(
            lambda sess: sess.get(HOME, timeout=25),
            cookie=cookie_str,
            headers=headers,
        )
        html = resp.text or ""
        if looks_blocked(resp.status_code, html, resp.headers):
            raise RuntimeError(self._cf_message())
        if resp.status_code >= 500:
            raise RuntimeError(f"打开签到首页返回 HTTP {resp.status_code}，站点可能正在维护")
        if resp.status_code != 200:
            raise RuntimeError(f"打开签到首页失败 HTTP {resp.status_code}")
        return html, session

    @staticmethod
    def _classify(data: dict) -> tuple[str, str, object, object]:
        """返回 (状态, 提示, 连续天数, 本次魔力)。状态：ok / done / login / csrf / fail。

        探测只确认了 ``state == "success"`` 是成功，其他 state 字符串没有抓全，
        所以先用文案宽泛识别「已签到 / 未登录 / CSRF 失败」，再剩下的一律当失败，
        绝不把没见过的 state 直接当成成功。

        例外：签到插件对「今天已经签过」的重复请求返回裸 ``{"state": false}``
        （布尔 False、无 message）。``str(False or "")`` 会把它吞成空串，旧代码
        因此落进 fail、误报成「签到失败：接口返回 state=False」。严格限定为
        布尔 False 且完全没有提示文案才判「今日已签到」，以免把带未知失败文案的
        state=False 误报成功。
        """
        raw_state = data.get("state")
        state = str(raw_state or "").strip().lower()
        message = str(data.get("message") or data.get("msg") or data.get("info") or "").strip()
        days = data.get("signindays")
        integral = data.get("integral")
        blob = f"{state} {message}".lower()
        if state == OK_STATE:
            return "ok", message, days, integral
        if any(hint in blob for hint in LOGIN_HINTS):
            return "login", message, days, integral
        if any(hint in blob for hint in DONE_HINTS):
            return "done", message, days, integral
        if any(hint in blob for hint in CSRF_HINTS):
            return "csrf", message, days, integral
        if raw_state is False and not message:
            return "done", "今日已签到（重复签到）", days, integral
        return "fail", message, days, integral

    # ------------------------------------------------------------------ #
    def run(self) -> list[Line]:
        html, session = self._open_home()

        csrf = self._extract_csrf(html)
        if not csrf:
            if self._is_login_page(html):
                raise RuntimeError(
                    "Cookie 已失效：签到首页是登录页，没有签到入口。"
                    "请重新登录 hdwing.org 后整行复制 Cookie（必须含 hdchina）"
                )
            raise RuntimeError(
                "签到首页里找不到 <meta name='x-csrf'>（签到必须每次现取 csrf）。"
                "站点模板可能改版了，请手动打开首页确认签到按钮还在"
            )

        lines: list[Line] = []
        who = self._who(html)
        if who:
            lines.append(Line("账号", who))

        # POST 复用取 csrf 时的同一个会话：token 和 CF 会话都与它绑定。
        # headers 用 update 而不是在 post 时另传，避免 curl_cffi 指纹模式下
        # 混进一套和 TLS 指纹矛盾的 client hints。
        session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": HOME,
            "Origin": BASE,
        })
        resp = session.post(SIGN_API, data={"csrf": csrf}, timeout=25, allow_redirects=False)
        body = resp.text or ""

        if looks_blocked(resp.status_code, body, resp.headers):
            raise RuntimeError(self._cf_message())
        if resp.status_code in (301, 302, 303, 307, 308):
            where = str(resp.headers.get("Location") or "")
            if "login" in where.lower():
                raise RuntimeError("签到请求被重定向到登录页，Cookie 已失效，重新复制一次")
            raise RuntimeError(f"签到请求被重定向（HTTP {resp.status_code} → {where[:80]}），站点接口可能改版")
        if resp.status_code >= 500:
            raise RuntimeError(f"签到接口返回 HTTP {resp.status_code}，站点可能正在维护")

        data = self._json(body)
        if data is None:
            if self._is_login_page(body):
                raise RuntimeError("签到接口返回的是登录页，Cookie 已失效")
            snippet = self._plain(body)[:160] or "（空）"
            raise RuntimeError(
                f"签到接口返回的不是 JSON（HTTP {resp.status_code}）：{snippet} —— "
                "插件可能改版，请手动去站点确认签到了没有"
            )

        state, message, days, integral = self._classify(data)
        if state == "login":
            raise RuntimeError(f"签到被拒：{message or '需要登录'} —— Cookie 已失效，重新复制一次")
        if state == "csrf":
            raise RuntimeError(
                f"签到被拒：csrf 校验失败（{message or 'token 可能过期'}）。"
                "程序已做到每次现取 token，反复出现说明站点模板改版或会话被重置，请手动确认"
            )
        if state == "fail":
            detail = message or ("接口返回 state=" + repr(data.get("state")))
            raise RuntimeError(f"签到失败：{detail}")

        lines.append(Line("签到", message or ("签到成功" if state == "ok" else "今日已签到")))
        if days is not None and str(days).strip().isdigit():
            lines.append(Line("连续签到", f"{int(days)} 天"))
        if integral is not None and str(integral).strip().lstrip("-").isdigit():
            lines.append(Line("本次魔力", str(integral).strip()))
        return lines

    # ------------------------------------------------------------------ #
    def verify(self) -> tuple[bool, str]:
        """只读检测：能拿到带 x-csrf 的正常首页即视为登录态有效，不点签到。"""
        try:
            html, _ = self._open_home()
        except RuntimeError as exc:
            # browser_request 抛的是 CF 排查指引，原样展示；别误报成 cookie 失效
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"连不上站点：{exc}"

        if self._is_login_page(html):
            return False, "Cookie 已失效（签到首页是登录页，没有签到入口）"
        if not self._extract_csrf(html):
            return False, "首页找不到 x-csrf，无法确认登录态（站点可能改版）"
        who = self._who(html)
        return True, f"Cookie 有效：{who}" if who else "Cookie 有效（首页有签到入口）"
