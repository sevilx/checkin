"""吾爱破解（www.52pojie.cn）：Discuz! X 任务系统签到。

**这个站挂的是瑞数（WZWS）动态防护，不是 Cloudflare。**

区别很关键：Cloudflare 看 TLS 指纹，换个 `curl_cffi` 指纹就能绕；瑞数是让浏览器
执行一段混淆 JS 算出一次性参数，再 POST 到 `/waf_zw_verify` 换一个短时效 cookie。
纯 HTTP 客户端（requests / curl_cffi）算不出来 —— 这也是为什么 GitHub 上那几个
52pojie 签到脚本全都标注了"已失效"。

实测现状（2026-11）：
  * `forum.php`、`home.php` -> 返回 32KB 的 JS 挑战页（含 WZWS 字样），拿不到内容
  * `plugin.php?...`        -> 不拦，但吾爱没有签到插件，返回"插件不存在"
  * 签到本身走任务系统：`home.php?mod=task&do=apply&id=2`（申请）+ `do=draw`（领奖）

所以本模块能做的只有：
  1. 用你浏览器里的 Cookie 直接请求任务接口 —— 如果这段 Cookie 里带着仍然有效的
     瑞数凭证（`WZWSREL...` 之类），服务端会放行，签到成功；
  2. 被拦时明确告诉你"被瑞数挡了、需要浏览器环境"，而不是含糊地报"签到失败"。

瑞数凭证通常几十分钟到几小时就过期，所以这条路**不适合长期无人值守**。
真要每天稳定签到，只能用真浏览器（Playwright / DrissionPage）打开页面点一下，
代价是镜像会大 1GB 左右。
"""

from __future__ import annotations

import re

from ..core import Line, NAV_HEADERS, Site, looks_blocked

BASE = "https://www.52pojie.cn"
HOME = f"{BASE}/home.php"
# Discuz 任务：id=2 是站点配的"每日签到"任务；apply 申请，draw 领奖
APPLY = f"{HOME}?mod=task&do=apply&id=2&referer=//"
DRAW = f"{HOME}?mod=task&do=draw&id=2&referer=//"
TASK_LIST = f"{HOME}?mod=task"

NICK = re.compile(r'class="vwmy"[^>]*>([^<]{1,40})<')
MESSAGE_BOX = re.compile(r'id="messagetext"[^>]*>(.*?)</div>', re.S)

LOGIN_HINTS = ("尚未登录", "请先登录", "需要登录", "您需要先登录", "登录后才能", "请登录后")
DONE_HINTS = ("已经签到", "已签到", "不是进行中的任务", "已完成", "已经完成", "已领取", "已经领取")
OK_HINTS = ("恭喜", "签到成功", "任务完成", "奖励已发放", "获得")
# 任务本身不存在（id 被改了）
NO_TASK_HINTS = ("任务不存在", "指定的任务", "任务已关闭", "没有找到")


class Pojie(Site):
    key = "POJIE"
    name = "吾爱破解"
    fields = ("cookie",)
    doc = (
        "登录 52pojie.cn → F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（要含以 _2132_auth 和 _2132_saltkey 结尾的两条）；"
        "站点有瑞数动态防护，Cookie 里的 WZWS 凭证几小时就过期"
    )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _decode(resp) -> str:
        """站点是 GBK，显式解码。挑战页本身是 UTF-8，按 ASCII 特征判断不影响。"""
        enc = (resp.encoding or "").lower()
        if not enc or enc in ("iso-8859-1", "ascii"):
            enc = "gbk"
        try:
            return resp.content.decode(enc, "ignore")
        except (LookupError, UnicodeDecodeError):
            return resp.text or ""

    @staticmethod
    def _is_waf(html: str) -> bool:
        """判断是不是瑞数的 JS 挑战页，而不是正常页面。"""
        if not html:
            return False
        lowered = html.lower()
        if "wzws" in lowered:
            return True
        # 挑战页没有 <title>，只有一段混淆 JS 拼出来的 document.write
        return "<title>" not in lowered and "createelement" in lowered

    @staticmethod
    def _message(html: str) -> str:
        box = MESSAGE_BOX.search(html)
        if not box:
            return ""
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", box.group(1))).strip()

    def _waf_error(self) -> str:
        return (
            "被瑞数动态防护拦截（WZWS）：请求没到吾爱破解，拿到的是 JS 挑战页。\n"
            "这不是 cookie 失效 —— 瑞数要求浏览器执行一段混淆 JS 算出一次性参数，"
            "纯 HTTP 客户端做不到。三个办法：\n"
            "1) 从浏览器重新复制整段 Cookie，里面要带 WZWSREL 开头的动态凭证 —— "
            "但它几十分钟到几小时就过期，只适合临时手动跑一次；\n"
            "2) 在浏览器里打开 52pojie.cn 并完成一次访问，立刻回面板点「立即签到」；\n"
            "3) 想每天自动签到，只能上真浏览器（Playwright/DrissionPage），"
            "本项目当前不内置以免镜像膨胀 1GB。"
        )

    # ------------------------------------------------------------------ #
    def run(self) -> list[Line]:
        session = self.session(headers={**NAV_HEADERS, "Referer": f"{BASE}/"})

        first = session.get(APPLY, timeout=25)
        html = self._decode(first)
        if self._is_waf(html):
            raise RuntimeError(self._waf_error())
        if looks_blocked(first.status_code, html, first.headers):
            raise RuntimeError(f"请求被拦截（HTTP {first.status_code}），换个网络或代理再试")
        if first.status_code >= 500:
            raise RuntimeError(f"站点返回 HTTP {first.status_code}，可能在维护")

        msg_apply = self._message(html)
        if any(hint in msg_apply for hint in LOGIN_HINTS):
            raise RuntimeError(
                f"Cookie 已失效：{msg_apply} —— 重新登录后复制整段 Cookie，"
                "要含以 _2132_auth、_2132_saltkey 结尾的那两条"
            )
        if any(hint in msg_apply for hint in NO_TASK_HINTS):
            raise RuntimeError(
                f"任务不存在：{msg_apply} —— 站点的签到任务 id 可能改了（当前写死 id=2），"
                "请到 https://www.52pojie.cn/home.php?mod=task 看「每日签到」任务的真实 id"
            )

        nick = NICK.search(html)
        lines: list[Line] = []
        if nick:
            lines.append(Line("账号", nick.group(1).strip()))

        # 申请后领奖；两步都可能给出结论
        second = session.get(DRAW, timeout=25)
        html2 = self._decode(second)
        if self._is_waf(html2):
            raise RuntimeError(self._waf_error())
        msg_draw = self._message(html2)

        state, message = self._classify(msg_apply, msg_draw)
        if state == "login":
            raise RuntimeError(f"Cookie 已失效：{message} —— 重新复制一次")
        if state == "unknown":
            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html2)).strip()[:160]
            raise RuntimeError(
                "签到结果无法判断。"
                f"申请任务返回：{msg_apply or '（空）'}；领取奖励返回：{msg_draw or '（空）'}；"
                f"页面片段：{snippet or '（空）'} —— 请手动去站点确认"
            )

        lines.append(Line("签到", message or ("今日已签到" if state == "done" else "签到成功")))
        lines.append(
            Line("提示", "吾爱破解有瑞数防护，Cookie 里的动态凭证会过期，建议在浏览器访问后立即运行")
        )
        return lines

    # ------------------------------------------------------------------ #
    @staticmethod
    def _classify(msg_apply: str, msg_draw: str) -> tuple[str, str]:
        """返回 (状态, 提示)。状态：ok / done / login / unknown。"""
        both = f"{msg_apply} {msg_draw}"
        if any(hint in both for hint in LOGIN_HINTS):
            return "login", msg_draw or msg_apply
        # 先看成功，避免"恭喜，任务已完成"被 done 规则先吃掉
        if any(hint in msg_draw for hint in OK_HINTS):
            return "ok", msg_draw
        if any(hint in msg_apply for hint in OK_HINTS) and not msg_draw:
            return "ok", msg_apply
        if any(hint in both for hint in DONE_HINTS):
            return "done", msg_draw or msg_apply
        return "unknown", msg_draw or msg_apply

    # ------------------------------------------------------------------ #
    def verify(self) -> tuple[bool, str]:
        """只读检测：任务列表页能拿到用户名 / 提示块，说明 cookie 有效。"""
        session = self.session(headers={**NAV_HEADERS, "Referer": f"{BASE}/"})
        try:
            resp = session.get(TASK_LIST, timeout=20)
        except Exception as exc:  # noqa: BLE001
            return False, f"连不上站点：{exc}"
        html = self._decode(resp)
        if self._is_waf(html):
            return False, "被瑞数防护拦截，无法检测（Cookie 里的动态凭证可能已过期）"
        if looks_blocked(resp.status_code, html, resp.headers):
            return False, f"被拦截（HTTP {resp.status_code}）"
        if resp.status_code != 200:
            return False, f"检测失败：HTTP {resp.status_code}"
        nick = NICK.search(html)
        if nick:
            return True, f"Cookie 有效：{nick.group(1).strip()}"
        msg = self._message(html)
        if any(hint in msg for hint in LOGIN_HINTS):
            return False, f"Cookie 已失效：{msg}"
        return False, "页面里没找到登录标记，Cookie 可能不完整"
