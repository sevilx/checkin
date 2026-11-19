"""恩山无线论坛（www.right.com.cn）：Discuz! X3.5 + erling_qd（二灵签到）插件。

和前面几个 Discuz 站不一样的地方：

1. **签到入口是插件自带接口，不是 Discuz 任务系统**。
   页面上的「签到」链接被伪静态成 `erling_qd-sign_in.html`，实际动作是
   `POST plugin.php?id=erling_qd:action&action=sign`，参数只有一个 `formhash`，
   返回**裸 JSON**（`{"success":true,"message":"...","continuous_days":N}`），
   不是 HTML 提示块，所以不能照抄福利吧那种解析套路。

2. **`formhash` 必须和当前会话对得上，否则请求会被 Discuz 直接掐掉**。
   实测：不带 formhash 访问接口 -> 200 + `{"success":false,"message":"请先登录"}`；
   随便填一个 formhash -> **503** + 整页「您当前的访问请求当中含有非法字符，已经被系统拒绝」
   （即 Discuz 的 `request_tainting`）。也就是说这个接口给出的 503 是「会话/表单串不匹配」，
   **不是站点挂了**，不能当服务端错误处理。所以流程必须是：
   先用同一份 cookie 拉一次 forum.php，从页面里把 formhash 抠出来，再提交。

3. **不能拿「有没有 formhash」当登录判据**。实测未登录访问 forum.php，
   页面顶部的快捷登录框 `<input type="hidden" name="formhash" value="4e051cb3">`
   里就带着一个游客 formhash。拿它去提交，接口会老老实实回 `{"success":false,"message":"请先登录"}`。
   真正可靠的登录标记是**退出链接** `member.php?mod=logging&action=logout&formhash=xxx`
   和用户名 `class="vwmy"`——游客页这两个都没有。
   顺带一提：登录后退出链接里那个 formhash 就是当前会话的正确值，直接从这儿取最省事。

4. 站点挂了阿里云 ESA（`Server: ESA`），机房 IP 容易被 521 拦。
   README 里的说明也提到过：这类站建议在家宽环境的 NAS 上跑，Actions 上大概率失败。
"""

from __future__ import annotations

import json
import re

from .. import discuz
from ..core import Line, NAV_HEADERS, Site, XHR_HEADERS, looks_blocked

HOST = "https://www.right.com.cn"
BASE = f"{HOST}/forum"
FORUM = f"{BASE}/forum.php"
# 插件动作接口：POST formhash 即可，返回 JSON
SIGN_API = f"{BASE}/plugin.php?id=erling_qd:action&action=sign"
# 积分页（恩山币 / 积分）
CREDIT = f"{BASE}/home.php?mod=spacecp&ac=credit&showcredit=1"

# 登录后才有的退出链接：里面带的 formhash 就是当前会话的正确值，
# 同时也充当「已登录」的判据（游客页没有这个链接）。
LOGOUT_FORMHASH = re.compile(
    r"member\.php\?mod=logging(?:&amp;|&)action=logout(?:&amp;|&)formhash=([0-9a-zA-Z]{8})"
)

# 取 formhash 的其他位置，按可靠性排序
FORMHASH_PATTERNS = (
    LOGOUT_FORMHASH,
    # 表单隐藏域
    re.compile(r'name=["\']formhash["\'][^>]*?value=["\']([0-9a-zA-Z]{8})["\']'),
    re.compile(r'value=["\']([0-9a-zA-Z]{8})["\'][^>]*?name=["\']formhash["\']'),
    # 内联 JS
    re.compile(r"formhash\s*[:=]\s*['\"]([0-9a-zA-Z]{8})['\"]"),
    # 兜底：任意 formhash=xxx（含 &amp; 转义）
    re.compile(r"formhash=(?:&amp;)?([0-9a-zA-Z]{8})"),
)

NICK = re.compile(r'class="vwmy"[^>]*>([^<]{1,40})<')
MESSAGE_BOX = re.compile(r'id="messagetext"[^>]*>(.*?)</div>', re.S)
# Discuz 页面骨架特征：用来区分「真的没登录」和「拿到的是 WAF / 异常页」
DISCUZ_MARKERS = ("discuz", "forum.php", "scbar", "id=\"hd\"")

# 瑞数（WZWS）动态防护的 JS 挑战页特征。2026-11 起恩山首页偶尔返回这种页面：
# HTTP 200 + 一段混淆 JS（`var arg1='...';(function(a,c){var G=a0j,...`），
# 没有 <title>、没有 Discuz 骨架。`looks_blocked()` 只认 403/429/503，
# 挑战页是 200 会漏判，最后掉进「首页不像论坛页面」的含糊兜底，所以单独识别。
RUI_SHU_MARKERS = ("var arg1=", "var arg1 =", "createelement")
# 瑞数凭证：和 IP + 浏览器指纹绑定，过期后浏览器能重算、纯 HTTP 客户端不行。
WZWS_COOKIE_KEYS = ("wzws_cid", "wzws_sid")

LOGIN_HINTS = ("请先登录", "尚未登录", "您需要登录", "登录后才能", "请登录后")
DONE_HINTS = ("已经签到", "已签到", "签到过了", "今日已签到", "今天已经签到")
OK_HINTS = ("签到成功", "签到完成", "已成功签到", "获得奖励")
# Discuz 的表单串校验失败（request_tainting），见模块开头说明
TAINT_HINTS = ("非法字符", "已经被系统拒绝", "request_tainting")

# 恩山币 / 积分：两种页面结构都兜一下，取不到就跳过，不影响签到结论
COIN = (
    re.compile(r"恩山币[:：]?\s*</em>\s*([\d,]+)"),
    re.compile(r"恩山币[^0-9]{0,60}?([\d][\d,]*)"),
)
POINT = (
    re.compile(r"积分[:：]?\s*</em>\s*([\d,]+)"),
    re.compile(r"<em>积分[:：]?\s*</em>\s*([\d,]+)"),
)


class EnShan(Site):
    key = "ENSHAN"
    name = "恩山无线论坛"
    fields = ("cookie",)
    doc = (
        "登录 right.com.cn → F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（要含以 _2132_auth 和 _2132_saltkey 结尾的两条；站点上了瑞数防护，"
        "还要含 wzws_cid——它和出口 IP 绑定、几十分钟就过期，需在浏览器看到首页后立即同步）"
    )

    # ------------------------------------------------------------------ #
    def _guard(self, status: int, text: str, headers, what: str) -> None:
        if looks_blocked(status, text, headers):
            raise RuntimeError(f"{what}时被防火墙拦截（HTTP {status}），换个网络或代理再试")
        # 恩山挂在阿里云 ESA 后面，机房 IP 常被 520~524 拦
        if status in (520, 521, 522, 523, 524):
            raise RuntimeError(
                f"{what}返回 HTTP {status}：恩山的 WAF 拦截了这次请求。"
                "这站对机房 IP 不友好，建议在家宽环境的 NAS 上跑，别用云端 Actions"
            )

    @staticmethod
    def _is_ruishu(html: str) -> bool:
        """判断是不是瑞数的 JS 挑战页（HTTP 200，但正文是混淆 JS 拼出来的）。

        实测样本开头：`var arg1='EC174E...';(function(a,c){var G=a0j,...`。
        `var arg1=` 是瑞数挑战页的独有指纹（正常 Discuz 页绝不会有），单独即可判定；
        报错里的片段常在 `parseInt(G(0x1` 处被截断、看不到 `createElement`，
        所以不能要求两者同时出现。另留「无 <title> 但有 createElement」兜底，与 pojie 一致。
        """
        if not html:
            return False
        lowered = html.lower()
        if "var arg1=" in lowered or "var arg1 =" in lowered:
            return True
        return "<title>" not in lowered and "createelement" in lowered

    def _ruishu_cookies(self) -> tuple[bool, bool]:
        """当前 Cookie 里带没带瑞数凭证，决定报错措辞（过期 vs 根本没同步）。"""
        cookie = (self.cookie or "").lower()
        return ("wzws_cid=" in cookie, "wzws_sid=" in cookie)

    def _ruishu_error(self) -> str:
        has_cid, has_sid = self._ruishu_cookies()
        if has_cid or has_sid:
            head = (
                "被瑞数动态防护（WZWS）拦截：首页返回的是 JS 挑战页，不是恩山的论坛页面。\n"
                "你同步过来的瑞数凭证（wzws_cid / wzws_sid）**已过期**——它和出口 IP 绑定，"
                "通常几十分钟就失效，而 Discuz 的登录票据能撑几天，"
                "所以会出现「登录态还在、就是拿不到页面」这种情况。"
            )
        else:
            head = (
                "被瑞数动态防护（WZWS）拦截：首页返回的是 JS 挑战页，不是恩山的论坛页面。\n"
                "而且当前 Cookie 里**没有瑞数凭证**（wzws_cid / wzws_sid），"
                "说明同步时浏览器里恩山并非活跃会话，或复制的整段 Cookie 少了这两个字段。"
            )
        steps = (
            "处理办法：\n"
            "1) 在浏览器打开 https://www.right.com.cn/forum/forum.php ，**等首页真正显示出来**"
            "（不是一闪而过的验证页），说明浏览器已经重新算过挑战；\n"
            "2) 立刻运行 tools\\sync.cmd --sites enshan 把含最新 wzws_cid 的整段 Cookie 同步过来；\n"
            "3) 回面板点「立即签到」，最好在 60 秒内完成。"
        )
        note = (
            "说明：瑞数要求浏览器执行混淆 JS 算出一次性参数，requests / curl_cffi 都算不出来"
            "（吾爱破解实测两种客户端各试 3 次全被拦）。站点的挑战是按出口 IP 触发的："
            "云端 Actions 的机房 IP 更容易被挑战，家宽 NAS 上跑通常能带过。")
        return f"{head}\n{steps}\n{note}"

    @staticmethod
    def _formhash(html: str) -> str:
        for pattern in FORMHASH_PATTERNS:
            found = pattern.search(html)
            if found:
                return found.group(1)
        return ""

    @classmethod
    def _login_state(cls, html: str) -> tuple[bool, str]:
        """返回 (是否已登录, 可用的 formhash)。

        判据只认登录后才渲染的两样东西：退出链接、用户名。
        游客页**也有** formhash（快捷登录框里的隐藏域），拿它判登录会全部误判成有效。
        """
        logout = LOGOUT_FORMHASH.search(html)
        if logout:
            return True, logout.group(1)
        if NICK.search(html):
            return True, cls._formhash(html)
        return False, ""

    @staticmethod
    def _json_of(text: str) -> dict | None:
        """把响应体当 JSON 解。

        插件正常返回裸 JSON；万一以后开了 `inajax=1`，外面会套一层
        `<?xml><root><![CDATA[...]]></root>`，这里一并剥掉。
        """
        raw = (text or "").strip()
        if not raw:
            return None
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end <= start:
            return None
        body = raw[start : end + 1]
        # CDATA / HTML 实体转义会破坏 JSON，先还原常见几个
        body = (
            body.replace("&quot;", '"')
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _plain(html: str) -> str:
        box = MESSAGE_BOX.search(html or "")
        source = box.group(1) if box else (html or "")
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", source)).strip()

    # ------------------------------------------------------------------ #
    def run(self) -> list[Line]:
        session = self.session(headers={**NAV_HEADERS, "Referer": FORUM})

        page = session.get(FORUM, timeout=25)
        html = page.text or ""
        self._guard(page.status_code, html, page.headers, "打开论坛首页")
        # 瑞数挑战页是 HTTP 200，looks_blocked 认不出来，必须在登录判据之前单独拦。
        if self._is_ruishu(html):
            raise RuntimeError(self._ruishu_error())
        if page.status_code >= 500:
            raise RuntimeError(f"打开论坛首页返回 HTTP {page.status_code}，站点可能在维护")
        if page.status_code != 200:
            raise RuntimeError(f"打开论坛首页失败 HTTP {page.status_code}")

        logged_in, formhash = self._login_state(html)
        who = discuz.nickname(html)

        if not logged_in:
            lowered = html.lower()
            if not any(marker in lowered for marker in DISCUZ_MARKERS):
                snippet = self._plain(html)[:120]
                raise RuntimeError(
                    f"首页返回的内容不像论坛页面（{snippet or '空响应'}），"
                    "可能被 WAF 拦截或站点正在改版，稍后重试"
                )
            if any(hint in html for hint in LOGIN_HINTS):
                raise RuntimeError(
                    f"Cookie 已失效（首页是游客态）。{discuz.cookie_hint('恩山')}"
                )
            raise RuntimeError(
                f"Cookie 已失效或不完整：首页没有登录态。{discuz.cookie_hint('恩山')}"
            )

        if not formhash:
            raise RuntimeError(
                "已登录但页面里取不到 formhash（签到必须要它）。"
                "站点模板可能改版了，先手动打开论坛确认能正常签到，再把情况反馈一下"
            )

        lines: list[Line] = []
        if who:
            lines.append(Line("账号", who))
        else:
            lines.append(Line("登录态", f"已登录（formhash {formhash[:2]}**）"))

        state, message, days = self._submit(session, formhash)

        if state == "login":
            raise RuntimeError(f"签到被拒：{message or '请先登录'} —— Cookie 已失效，重新复制一次")
        if state == "stale":
            raise RuntimeError(
                "签到接口返回「请求含有非法字符」（Discuz 的 formhash 校验失败）。"
                "一般是 cookie 里的登录票据已经过期，会话换了新的。重新复制一次 Cookie 即可"
            )
        if state == "unknown":
            raise RuntimeError(
                "签到结果无法判断（接口既没给 JSON，也没给可识别的提示）。"
                f"返回内容：{message or '（空）'} —— 请手动去站点确认"
            )

        if state == "done":
            lines.append(Line("签到", message or "今日已签到"))
        else:
            lines.append(Line("签到", message or "签到成功"))

        if isinstance(days, (int, str)) and str(days).strip().isdigit():
            lines.append(Line("连续签到", f"{int(days)} 天"))

        # 附加信息：恩山币 / 积分（取不到就算了，不算失败）
        for name, value in self._credit(session):
            lines.append(Line(name, value))

        return lines

    # ------------------------------------------------------------------ #
    def _submit(self, session, formhash: str) -> tuple[str, str, object]:
        """提交签到，返回 (状态, 提示, 连续天数)。状态：ok / done / login / stale / unknown。"""
        resp = session.post(
            SIGN_API,
            data={"formhash": formhash},
            timeout=25,
            headers={
                **XHR_HEADERS,
                "Referer": FORUM,
                "Origin": HOST,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        text = resp.text or ""
        self._guard(resp.status_code, text, resp.headers, "提交签到")

        data = self._json_of(text)
        if data is not None:
            message = str(data.get("message") or "").strip()
            days = data.get("continuous_days")
            if data.get("success"):
                return "ok", message or "签到成功", days
            if any(hint in message for hint in LOGIN_HINTS):
                return "login", message, days
            if any(hint in message for hint in DONE_HINTS):
                return "done", message or "今日已签到", days
            # success=false 但原因不明：插件把「今天已签过」也走这条路时靠关键字兜
            if any(hint in text for hint in DONE_HINTS):
                return "done", message or "今日已签到", days
            return "unknown", message or f"HTTP {resp.status_code}", days

        plain = self._plain(text)

        if any(hint in text for hint in TAINT_HINTS):
            return "stale", plain[:160], None
        if any(hint in text for hint in LOGIN_HINTS):
            return "login", plain[:160], None
        if any(hint in text for hint in DONE_HINTS):
            return "done", plain[:160] or "今日已签到", None
        if any(hint in text for hint in OK_HINTS):
            return "ok", plain[:160] or "签到成功", None
        return "unknown", plain[:160], None

    # ------------------------------------------------------------------ #
    def _credit(self, session) -> list[tuple[str, str]]:
        """读恩山币 / 积分。纯展示用，任何异常都吞掉，绝不影响签到结论。"""
        try:
            resp = session.get(CREDIT, timeout=20, headers={"Referer": FORUM})
        except Exception:  # noqa: BLE001
            return []
        if resp.status_code != 200:
            return []
        html = resp.text or ""

        out: list[tuple[str, str]] = []
        for name, patterns in (("恩山币", COIN), ("积分", POINT)):
            for pattern in patterns:
                found = pattern.search(html)
                if found:
                    out.append((name, found.group(1).replace(",", "")))
                    break
        return out

    # ------------------------------------------------------------------ #
    def verify(self) -> tuple[bool, str]:
        """只读检测：首页出现退出链接/用户名就说明登录票据还有效。

        注意不能用 formhash 判断 —— 游客页的快捷登录框里也带一个。
        """
        session = self.session(headers={**NAV_HEADERS, "Referer": FORUM})
        try:
            resp = session.get(FORUM, timeout=20)
        except Exception as exc:  # noqa: BLE001
            return False, f"连不上站点：{exc}"

        html = resp.text or ""
        if self._is_ruishu(html):
            has_cid, _ = self._ruishu_cookies()
            return False, (
                "被瑞数防护拦截，无法检测：Cookie 里的瑞数凭证已过期，重新同步一次"
                if has_cid else
                "被瑞数防护拦截，无法检测：Cookie 里没有瑞数凭证"
            )
        if looks_blocked(resp.status_code, html, resp.headers):
            return False, f"被防火墙拦截（HTTP {resp.status_code}）"
        if resp.status_code != 200:
            return False, f"检测失败：HTTP {resp.status_code}"

        logged_in, _ = self._login_state(html)
        if logged_in:
            return True, f"Cookie 有效：{discuz.nickname(html) or '已登录'}"
        return False, "Cookie 已失效（首页是游客态）"
