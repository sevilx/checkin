"""V2EX：/mission/daily 领取每日登录奖励。

V2EX 的"签到"其实是任务页上的 **领取铜币** 按钮，页面本身没有"签到"字样，
按钮文案和 DOM 结构也改过几次。所以这里不依赖按钮的 class 和标签名，
只在页面里找 `location.href = '/mission/daily/redeem?once=xxxx'` 这个跳转地址 ——
它才是真正触发领取的链接，结构怎么变都还在。

两种正常状态：
* 页面出现「每日登录奖励已领取」→ 今天已经领过（算成功，不是失败）；
* 找到 redeem 链接 → 请求它完成领取。

旧版本把"已经领过"当成失败抛了异常，会让每天都显示红色，这里修掉了。
"""

from __future__ import annotations

import re

import urllib3

from ..core import Line, NAV_HEADERS, Site, looks_blocked

urllib3.disable_warnings()

BASE = "https://www.v2ex.com"
DAILY_URL = f"{BASE}/mission/daily"
BALANCE_URL = f"{BASE}/balance"

REDEEM = re.compile(r"(/mission/daily/redeem\?once=\d+)")
DONE_HINTS = ("每日登录奖励已领取", "已领取", "已连续登录")
LOGIN_HINTS = ("需要先登录", "请先登录", "/signin")


class V2ex(Site):
    key = "V2EX"
    name = "V2EX 论坛"
    fields = ("cookie", "a2", "proxy")
    required = ()
    doc = "登录 v2ex.com → F12 → Cookie 整段复制；也可以只填 A2（V2EX_A2=xxx）"

    def __init__(self, account: dict, index: int = 0):
        super().__init__(account, index)
        if not self.account.get("cookie") and not self.account.get("a2"):
            raise ValueError("缺少必填配置: cookie 或 a2")
        if not self.account.get("cookie"):
            self.account["cookie"] = f"A2={self.account['a2']}; V2EX_LANG=zhcn"

    # ------------------------------------------------------------------ #
    def run(self) -> list[Line]:
        session = self.session(headers={**NAV_HEADERS, "Referer": f"{BASE}/"})

        page = session.get(DAILY_URL, verify=False, timeout=25)
        if page.status_code in (403, 429) and looks_blocked(page.status_code, page.text):
            raise RuntimeError(
                f"被 Cloudflare 风控拦了（HTTP {page.status_code}）：当前出口 IP 触发限制，"
                "换一个代理出口或过一会儿再试"
            )
        if "/signin" in str(page.url) or any(h in page.text for h in LOGIN_HINTS):
            raise RuntimeError("Cookie 已失效（被重定向到登录页）")

        text = page.text
        if any(hint in text for hint in DONE_HINTS):
            claimed = "今日已领取（无需重复）"
        else:
            match = REDEEM.search(text)
            if not match:
                raise RuntimeError(
                    "任务页里既没有「已领取」也没有领取链接，页面结构可能变了。"
                    "先在浏览器打开 https://www.v2ex.com/mission/daily 确认能正常领取，再回来试"
                )
            target = match.group(1)
            resp = session.get(
                BASE + target,
                verify=False,
                timeout=25,
                headers={"Referer": DAILY_URL},
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"领取请求失败 HTTP {resp.status_code}")
            claimed = "已领取每日登录奖励"

        return [Line("账号", self._username(session)), Line("今日签到", claimed)] + self._balance(session)

    # ------------------------------------------------------------------ #
    def _username(self, session) -> str:
        try:
            data = session.get(f"{BASE}/api/v2/member/me", verify=False, timeout=20).json()
            user = data.get("result") if isinstance(data.get("result"), dict) else data
            name = (user or {}).get("username")
            if name:
                return str(name)
        except Exception:  # noqa: BLE001
            pass
        try:
            html = session.get(BALANCE_URL, verify=False, timeout=20).text
            found = re.search(r'<a href="/member/[^"]+"[^>]*class="top"[^>]*>(.*?)</a>', html)
            if found:
                return found.group(1).strip()
        except Exception:  # noqa: BLE001
            pass
        return "未知（已登录）"

    def _balance(self, session) -> list[Line]:
        """余额读不到不算失败，签到本身已经成功了。"""
        try:
            html = session.get(BALANCE_URL, verify=False, timeout=20).text
        except Exception:  # noqa: BLE001
            return []

        lines: list[Line] = []
        coins = re.search(r'<td class="d"[^>]*>\s*(\d+(?:\.\d+)?)\s*</td>', html)
        if coins:
            lines.append(Line("账户余额", f"{coins.group(1)} 铜币"))

        try:
            daily = session.get(DAILY_URL, verify=False, timeout=20).text
            days = re.search(r"已连续登录\s*(\d+)\s*天", daily) or re.search(
                r"连续登录[^0-9]{0,8}(\d+)\s*天", daily
            )
            if days:
                lines.append(Line("连续登录", f"{days.group(1)} 天"))
        except Exception:  # noqa: BLE001
            pass
        return lines

    def verify(self) -> tuple[bool, str]:
        """只读检测：/api/v2/member/me，200 表示 A2 有效。"""
        session = self.session(headers={**NAV_HEADERS, "Referer": f"{BASE}/"})
        resp = session.get(f"{BASE}/api/v2/member/me", verify=False, timeout=20)
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return False, "接口返回非 JSON，可能被拦截，换网络再试"
            user = data.get("result") if isinstance(data.get("result"), dict) else data
            name = (user or {}).get("username") or "已登录"
            return True, f"Cookie 有效：{name}"
        if resp.status_code in (401, 403):
            if looks_blocked(resp.status_code, resp.text):
                return False, "被 Cloudflare 拦截（HTTP 403），换个出口 IP 再试"
            return False, "Cookie 已失效（接口要求登录）"
        return False, f"检测失败：HTTP {resp.status_code}"
