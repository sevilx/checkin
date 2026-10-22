"""V2EX：/mission/daily 领取每日登录奖励，并读取余额与连续签到天数。"""

from __future__ import annotations

import re

import urllib3

from ..core import Line, Site, parse_cookie

urllib3.disable_warnings()

SIGN_BUTTON = re.compile(
    r'<input type="button" class="super normal button" value=".*?" onclick="location\.href = \'(.*?)\';" />'
)


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

    def run(self) -> list[Line]:
        session = self.session(
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )

        page = session.get("https://www.v2ex.com/mission/daily", verify=False, timeout=25)
        if "/signin" in page.url or "需要先登录" in page.text:
            raise RuntimeError("Cookie 已失效（被重定向到登录页）")

        urls = SIGN_BUTTON.findall(page.text)
        if not urls:
            # 找不到按钮：可能已签到，也可能是页面改版
            balance = session.get("https://www.v2ex.com/balance", verify=False, timeout=25).text
            if "/signin" in balance and "登录" in balance:
                raise RuntimeError("Cookie 已失效")
            raise RuntimeError("未找到签到按钮，可能今日已签到或页面改版")

        target = urls[0]
        if target != "/balance":
            session.get(
                "https://www.v2ex.com" + target,
                verify=False,
                timeout=25,
                headers={"Referer": "https://www.v2ex.com/mission/daily"},
                params={"once": target.split("=")[-1]},
            )

        balance_page = session.get("https://www.v2ex.com/balance", verify=False, timeout=25).text
        total = re.search(r'<td class="d" style="text-align: right;">(\d+\.\d+)</td>', balance_page)
        today = re.search(r'<td class="d"><span class="gray">(.*?)</span></td>', balance_page)
        username = re.search(r'<a href="/member/.*?" class="top">(.*?)</a>', balance_page)

        lines = [Line("账号", username.group(1) if username else "未知")]
        lines.append(Line("今日签到", today.group(1) if today else "已签到"))
        lines.append(Line("账户余额", f"{total.group(1)} 铜币" if total else "读取失败"))

        daily = session.get("https://www.v2ex.com/mission/daily", verify=False, timeout=25).text
        days = re.search(r'<div class="cell">(.*?)天</div>', daily)
        if days:
            lines.append(Line("连续签到", f"{days.group(1)} 天"))
        return lines


    def verify(self) -> tuple[bool, str]:
        """只读检测：/api/v2/member/me，200 表示 A2 有效。"""
        session = self.session(
            headers={"Referer": "https://www.v2ex.com/", "Accept": "application/json"}
        )
        resp = session.get("https://www.v2ex.com/api/v2/member/me", timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            user = data.get("result") if isinstance(data.get("result"), dict) else data
            name = (user or {}).get("username") or "已登录"
            return True, f"Cookie 有效：{name}"
        if resp.status_code in (401, 403):
            return False, "Cookie 已失效（接口要求登录）"
        return False, f"检测失败：HTTP {resp.status_code}"
