"""福利吧论坛（www.wnflb2023.com）：Discuz + fx_checkin（福吧签到）插件。

和飞牛论坛的 zqlj_sign 不是一个东西，不能照抄：

* 打卡地址**每次都变**。页面里是
  `showWindow('fx_checkin', 'plugin.php?id=fx_checkin:checkin&formhash=xxx&yyy')`，
  后面的 hash 是这次渲染现生成的，写死一个值第二天就失效。
  所以流程必须是「先拉 forum.php → 把地址原样抠出来 → 再请求」。
* 页面里到处都有「签到成功!」这四个字（插件把弹窗模板内联在每一页），
  拿它当成功判据会永远误报成功。真正的结果在 Discuz 的提示块
  `<div id="messagetext">` 里，只能认那一块。
"""

from __future__ import annotations

import random
import re
import time
from urllib.parse import urljoin

from .. import discuz
from ..core import Line, NAV_HEADERS, Site, looks_blocked

BASE = "https://www.wnflb2023.com"
FORUM = f"{BASE}/forum.php"

# 页面给签到窗口用的地址（相对路径，可能带 &amp; 转义）
CHECKIN_JS = re.compile(r"showWindow\('fx_checkin',\s*'([^']+)'\)")
CHECKIN_HREF = re.compile(r'href="(plugin\.php\?id=fx_checkin:checkin[^"]*)"')
FX_FORMHASH = re.compile(r'FX_FORMHASH\s*=\s*"([0-9a-fA-F]+)"')
PLAIN_FORMHASH = re.compile(r"formhash=([0-9a-fA-F]{8,})")

# Discuz 提示块：登录失效、已签到、签到成功都走这里
MESSAGE_BOX = re.compile(r'id="messagetext"[^>]*>(.*?)</div>', re.S)
SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
# 登录态判断统一走 checkin.discuz：福利吧的用户名藏在 <strong class="vwmy"><a>名字</a></strong>
# 的子元素里，本文件原来的 NICK 正则要求 vwmy 后紧跟文本，会把有效 cookie 判成失效。
LOGIN_HINTS = discuz.GUEST_HINTS
DONE_HINTS = ("已经签到", "已签到", "签到过了", "已打过卡", "今日已签到")
OK_HINTS = ("签到成功", "签到完成", "已成功签到", "签到获得", "获得奖励")

# 这几条只在真正打卡成功后出现，模板里没有，用来兜底
STRONG_OK = (
    re.compile(r"今天第\s*(\d+)\s*个签到"),
    re.compile(r"您是第\s*(\d+)\s*个"),
)

CONSEC = re.compile(r"已连续签到[^0-9]{0,6}(\d+)[^0-9]{0,4}天")
TOTAL = re.compile(r"累计签到[^0-9]{0,6}(\d+)[^0-9]{0,4}天")


class WnflbForum(Site):
    key = "WNFLB"
    name = "福利吧论坛"
    fields = ("cookie",)
    doc = (
        "登录 wnflb2023.com → F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（要含以 _auth 和 _saltkey 结尾的两条）"
    )

    # ------------------------------------------------------------------ #
    def _get(self, session, url: str, **kwargs):
        """5xx 退避重试 3 次：这个站偶尔会突然 502 / 504。"""
        resp = None
        for attempt in range(3):
            resp = session.get(url, timeout=25, **kwargs)
            if resp.status_code < 500:
                return resp
            if attempt < 2:
                time.sleep(1.5 + attempt * 2.0)
        return resp

    def _check(self, resp, what: str) -> None:
        if looks_blocked(resp.status_code, resp.text, resp.headers):
            raise RuntimeError(f"{what}时被防火墙拦截（HTTP {resp.status_code}），换个网络或代理再试")
        if resp.status_code >= 500:
            raise RuntimeError(f"{what}返回 HTTP {resp.status_code}（已重试 3 次），站点可能在维护")
        if resp.status_code != 200:
            raise RuntimeError(f"{what}失败 HTTP {resp.status_code}")

    # ------------------------------------------------------------------ #
    def run(self) -> list[Line]:
        session = self.session(
            headers={
                **NAV_HEADERS,
                "Referer": FORUM,
            }
        )

        page = self._get(session, FORUM)
        self._check(page, "打开论坛首页")

        html = page.text or ""
        ok, why = discuz.login_verdict(html, "福利吧")
        if not ok:
            raise RuntimeError(why)

        who = discuz.nickname(html)
        lines: list[Line] = [Line("账号", who)] if who else []

        url = self._checkin_url(html)
        resp = self._get(session, url, headers={"Referer": FORUM})
        self._check(resp, "提交签到")

        state, message, raw = self._classify(resp.text or "")
        if state == "login":
            raise RuntimeError(f"签到被拒：{message} —— Cookie 已失效，{discuz.cookie_hint('福利吧')}")
        if state == "unknown":
            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()[:160]
            raise RuntimeError(
                "签到结果无法判断（既没看到成功也没看到已签到）。"
                f"页面返回：{snippet or '（空）'} —— 可能是插件改版，请手动去站点确认"
            )

        if state == "done":
            lines.append(Line("签到", message or "今日已签到"))
        else:
            lines.append(Line("签到", message or "签到成功"))

        # 附加信息：连续 / 累计 / 排名（能解析到就显示，解析不到不报错）
        for name, pattern, unit in (
            ("连续签到", CONSEC, "天"),
            ("累计签到", TOTAL, "天"),
        ):
            found = pattern.search(raw)
            if not found:
                found = pattern.search(html)
            if found:
                lines.append(Line(name, f"{found.group(1)} {unit}"))

        return lines

    # ------------------------------------------------------------------ #
    def _checkin_url(self, html: str) -> str:
        """从首页抠出这次可用的打卡地址，抠不到就用页面里的 hash 拼一个。"""
        match = CHECKIN_JS.search(html) or CHECKIN_HREF.search(html)
        if match:
            path = match.group(1).replace("&amp;", "&").strip()
            return urljoin(FORUM, path)

        fx = FX_FORMHASH.search(html)
        plain = PLAIN_FORMHASH.search(html)
        if fx or plain:
            token = (plain.group(1) if plain else "") or (fx.group(1) if fx else "")
            extra = fx.group(1) if fx and fx.group(1) != token else ""
            path = f"plugin.php?id=fx_checkin:checkin&formhash={token}"
            if extra:
                path += f"&{extra}"
            return urljoin(FORUM, path)

        raise RuntimeError("首页里没找到签到入口（formhash 也没有），插件可能被关闭或改版")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _message_text(html: str) -> str:
        """取 Discuz 提示块正文，顺手去掉内联 script/style。

        插件会在提示块里塞一段自动跳转的 JS，不清理的话日志会变成
        「您今日已签到，无需重复签到！ if(history.length > ...) document.write(...)」。
        """
        box = MESSAGE_BOX.search(html)
        if not box:
            return ""
        raw = SCRIPT_STYLE.sub(" ", box.group(1))
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()

    def _classify(self, html: str) -> tuple[str, str, str]:
        """返回 (状态, 提示语, 原始 html)。状态：ok / done / login / unknown。"""
        message = self._message_text(html)

        # 有提示块就以它为准 —— 每一页都内联了"签到成功!"模板，扫全文必误判
        if message:
            if any(hint in message for hint in LOGIN_HINTS):
                return "login", message, html
            if any(hint in message for hint in DONE_HINTS):
                return "done", message, html
            if any(hint in message for hint in OK_HINTS):
                return "ok", message, html
            return "unknown", message, html

        # 没有提示块（插件直接吐弹窗内容）时，只认模板里不可能出现的强特征
        for pattern in STRONG_OK:
            found = pattern.search(html)
            if found:
                return "ok", f"签到成功，今天第 {found.group(1)} 个", html
        if any(hint in html for hint in DONE_HINTS):
            return "done", "今日已签到", html
        return "unknown", "", html

    # ------------------------------------------------------------------ #
    def verify(self) -> tuple[bool, str]:
        """只读检测：首页有退出链接就说明 cookie 有效。"""
        session = self.session(headers={**NAV_HEADERS, "Referer": FORUM})
        try:
            resp = self._get(session, FORUM)
        except Exception as exc:  # noqa: BLE001
            return False, f"连不上站点：{exc}"
        if looks_blocked(resp.status_code, resp.text, resp.headers):
            return False, f"被防火墙拦截（HTTP {resp.status_code}）"
        if resp.status_code != 200:
            return False, f"检测失败：HTTP {resp.status_code}"
        return discuz.login_verdict(resp.text or "", "福利吧")
