"""盘就是六（pan946.com）：Xiuno BBS 的每日签到。

这个站和项目里其他论坛都不一样，不能套 Discuz 那套：

* **没有 formhash，也没有 plugin.php**。签到按钮就是导航栏上一个普通链接，
  点了之后前端调用 `jQuery.xpost(xn.url('my-sign'), '')` —— 也就是
  `POST /my-sign.htm`，请求体是空字符串，响应是一段 JSON：`{"code":0,"message":"..."}`。
  所以这里既不用抠 hash，也不用解析 HTML 提示块，拿 code 判断就行。
* **未登录时一律 302 到 `user-login.htm`**（GET / POST 都一样）。
  这反而是最干净的判据：不用猜关键字，看有没有被重定向就够了。
* **登录只要邮箱 + 密码**，页面上没有验证码、没有滑块（只有 QQ/微信快捷登录可选）。
  所以这个站可以做成"账号密码模式"，每天现登现签，不用像 Discuz 那样担心
  cookie 过期 —— 这也是本模块优先推荐密码登录的原因。

凭据二选一：
  * `email` + `password`：推荐，长期免维护；
  * `cookie`：懒得给密码就从浏览器整行复制，但 Xiuno 的登录票据也会过期，
    过期后表现为"签到页被重定向到登录页"，程序会明确告诉你。
"""

from __future__ import annotations

import json
import re

from ..core import Line, NAV_HEADERS, Site, XHR_HEADERS, looks_blocked

BASE = "https://pan946.com"
HOME = f"{BASE}/"
LOGIN = f"{BASE}/user-login.htm"
SIGN = f"{BASE}/my-sign.htm"

# 签到成功后页面/JSON 里可能带的统计，能解析到就显示，解析不到不报错
CONSEC = re.compile(r"连[续签]{1,2}[^0-9]{0,6}(\d+)[^0-9]{0,4}天")
TOTAL = re.compile(r"累计[^0-9]{0,6}(\d+)[^0-9]{0,4}天")
RANK = re.compile(r"第\s*(\d+)\s*[个名]")

DONE_HINTS = ("已经签到", "已签到", "签到过了", "今天已签", "重复签到")
LOGIN_HINTS = ("请先登录", "登录后", "请登录")


class Pan946(Site):
    key = "PAN946"
    name = "盘就是六"
    # cookie 与 邮箱密码 二选一，所以 required 留空、在 __init__ 里自己校验
    fields = ("cookie", "email", "password")
    required = ()
    doc = (
        "两种填法任选其一：① 填邮箱 + 密码（推荐，程序每天自己登录，不会过期）；"
        "② 登录 pan946.com 后 F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（要含 bbs_sid、bbs_uid、bbs_token）"
    )

    def __init__(self, account: dict, index: int = 0):
        super().__init__(account, index)
        has_cookie = bool(self.cookie)
        has_password = bool(self.account.get("email")) and bool(self.account.get("password"))
        if not has_cookie and not has_password:
            raise ValueError("需要 cookie，或者 email + password（二选一）")

    # ------------------------------------------------------------------ #
    # 登录：只在没给 cookie 时才走
    # ------------------------------------------------------------------ #
    def _login(self, session) -> None:
        """用邮箱密码登录，成功后 session 里会多出 bbs_uid / bbs_token。

        Xiuno 判断"浏览器支不支持 cookie"靠一个 `cookie_test` 探测值，
        所以一定要先 GET 一次登录页把 bbs_sid / cookie_test 拿全，再 POST，
        否则服务端会当成不支持 cookie 的客户端处理。
        """
        email = str(self.account.get("email") or "").strip()
        password = str(self.account.get("password") or "")
        if not email or not password:
            raise RuntimeError("没有可用的 cookie，也没有填邮箱/密码")

        session.get(LOGIN, timeout=25, headers={**NAV_HEADERS, "Referer": HOME})
        resp = session.post(
            LOGIN,
            data={"email": email, "password": password},
            headers={
                **NAV_HEADERS,
                "Referer": LOGIN,
                "Origin": BASE,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=25,
            allow_redirects=False,
        )
        if looks_blocked(resp.status_code, resp.text or "", resp.headers):
            raise RuntimeError(f"登录时被 Cloudflare 拦截（HTTP {resp.status_code}），换个网络或给该站点配代理")

        jar = session.cookies.get_dict()
        if "bbs_uid" in jar or "bbs_user" in jar:
            return
        # 登录失败：站点回 200，正文是 Xiuno 的提示模板
        raise RuntimeError(self._login_error(resp))

    def _login_error(self, resp) -> str:
        """从失败页里抠出提示语。

        这个站的错误提示长这样（Xiuno 的 message 模板）：
            <h4 class="card-title text-center mb-0"><i class="icon-ok"></i> 邮箱不存在 </h4>
        剥掉标签就是"邮箱不存在"。之前退而取 <title> 会拿到站点名"盘就是六"，
        对排查毫无帮助，所以这里优先认 h4，认不到才兜底。
        """
        text = resp.text or ""
        detail = ""
        for pattern in (r"<h4[^>]*>(.*?)</h4>", r'class="[^"]*(?:alert|invalid-feedback|error)[^"]*"[^>]*>(.*?)</'):
            box = re.search(pattern, text, re.S)
            if not box:
                continue
            detail = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", box.group(1))).strip()
            if detail:
                break
        if not detail or detail == self.name:
            detail = "邮箱或密码不正确"
        hint = (
            "如果确认密码没错，可能是站点加了图形验证码，这时改用 cookie 方式："
            "浏览器登录后复制整行 Cookie 填到设置页"
        )
        return f"登录失败：{detail} —— {hint}"

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _session(self):
        return self.session(
            headers={
                **NAV_HEADERS,
                "Referer": HOME,
            }
        )

    @staticmethod
    def _json(resp) -> dict:
        text = (resp.text or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _text_of(html: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()

    def _require_login(self, session) -> None:
        """GET 签到页，被重定向到登录页就说明凭据已经不能用了。"""
        resp = session.get(SIGN, headers={"Referer": HOME}, timeout=25, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            where = str(resp.headers.get("Location") or "")
            if "user-login" in where or "login" in where:
                raise RuntimeError(
                    "Cookie 已失效：签到页被重定向到登录页。\n"
                    "盘就是六是 Xiuno 论坛，登录票据一样会过期；"
                    "改用邮箱+密码登录就不用管这个了（设置页把 cookie 清空、填 email 和 password）"
                )
            # 其他跳转：跟着走一次
            resp = session.get(SIGN, headers={"Referer": HOME}, timeout=25, allow_redirects=True)
        if looks_blocked(resp.status_code, resp.text or "", resp.headers):
            raise RuntimeError(f"被 Cloudflare 拦截（HTTP {resp.status_code}），换个网络或给该站点配代理")
        if resp.status_code >= 500:
            raise RuntimeError(f"签到页返回 HTTP {resp.status_code}，站点可能在维护")
        if resp.status_code != 200:
            raise RuntimeError(f"打开签到页失败：HTTP {resp.status_code}")

    # ------------------------------------------------------------------ #
    def run(self) -> list[Line]:
        session = self._session()
        if not self.cookie:
            self._login(session)

        self._require_login(session)

        resp = session.post(
            SIGN,
            data="",
            headers={
                **XHR_HEADERS,
                "Referer": SIGN,
                "Origin": BASE,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=25,
            allow_redirects=False,
        )

        if resp.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(
                "签到请求被重定向到登录页，凭据已失效。"
                "建议改用邮箱+密码登录（设置页填 email / password，清空 cookie）"
            )
        if looks_blocked(resp.status_code, resp.text or "", resp.headers):
            raise RuntimeError(f"被 Cloudflare 拦截（HTTP {resp.status_code}），换个网络或给该站点配代理")

        payload = self._json(resp)
        if not payload:
            snippet = self._text_of(resp.text or "")[:160]
            raise RuntimeError(
                f"签到接口返回的不是 JSON（HTTP {resp.status_code}）：{snippet or '（空）'} —— "
                "插件可能改版，请手动去站点确认"
            )

        code = payload.get("code")
        message = str(payload.get("message") or "").strip()
        state, message = self._classify(code, message)

        lines: list[Line] = [Line("账号", str(self.account.get("email") or "") or "（cookie 登录）")]
        if state == "login":
            raise RuntimeError(f"签到被拒：{message} —— 凭据已失效，建议改用邮箱+密码登录")
        if state == "fail":
            raise RuntimeError(f"签到失败：{message or f'接口返回 code={code}'}")
        lines.append(Line("签到", message or ("今日已签到" if state == "done" else "签到成功")))

        # 统计信息在签到页上，签到完重新拉一次最省事（顺便确认登录态还在）
        try:
            page = session.get(SIGN, headers={"Referer": HOME}, timeout=20)
            # 必须先剥标签再匹配：真实页面是「连续签到 <b>3</b> 天」，
            # 带尖括号时正则里的"数字后最多 4 个非数字"会被 "</b> " 撑爆。
            plain = self._text_of(page.text or "")
            for name, pattern, unit in (("连续签到", CONSEC, "天"), ("累计签到", TOTAL, "天")):
                found = pattern.search(plain)
                if found:
                    lines.append(Line(name, f"{found.group(1)} {unit}"))
            rank = RANK.search(message)
            if rank:
                lines.append(Line("今日排名", f"第 {rank.group(1)} 个"))
        except Exception:  # noqa: BLE001 - 统计拿不到不影响签到结果
            pass

        return lines

    @staticmethod
    def _classify(code, message: str) -> tuple[str, str]:
        """返回 (状态, 提示)。状态：ok / done / login / fail。

        先看文案再看 code：Xiuno 插件的 code 约定不统一，有的对"今天已签到"
        回 code=0 + 提示语，有的回非 0。文案能认出来就以文案为准，
        认不出来才按 code 判断 —— 非 0 一律算失败，不能自欺欺人报成功。
        """
        if any(hint in message for hint in DONE_HINTS):
            return "done", message
        if any(hint in message for hint in LOGIN_HINTS):
            return "login", message
        if str(code) == "0":
            return "ok", message
        return "fail", message

    # ------------------------------------------------------------------ #
    def verify(self) -> tuple[bool, str]:
        """只读检测：能正常打开签到页（没被重定向）就说明凭据可用。"""
        session = self._session()
        try:
            if not self.cookie:
                self._login(session)
        except RuntimeError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"连不上站点：{exc}"

        try:
            resp = session.get(SIGN, headers={"Referer": HOME}, timeout=20, allow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            return False, f"连不上站点：{exc}"

        if resp.status_code in (301, 302, 303, 307, 308):
            return False, "Cookie 已失效：签到页被重定向到登录页"
        if looks_blocked(resp.status_code, resp.text or "", resp.headers):
            return False, f"被 Cloudflare 拦截（HTTP {resp.status_code}）"
        if resp.status_code != 200:
            return False, f"检测失败：HTTP {resp.status_code}"
        return True, "凭据有效，可以签到"
