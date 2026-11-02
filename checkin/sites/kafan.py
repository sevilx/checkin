"""卡饭论坛（bbs.kafan.cn）：Discuz! X + dsu_amupper（打卡机）插件。

两个和其他 Discuz 站不一样的地方：

1. **编码是 GBK**。响应头虽然写了 `charset=gbk`，requests 偶尔会按 ISO-8859-1 处理，
   中文关键字就匹配不上了，所以这里所有解析都用 `_decode()` 显式按 GBK 解。

2. **插件被改成了"自动签到"模式**。标准 dsu_amupper 的提交入口是
   `plugin.php?id=dsu_amupper:pper`，但卡饭这个模块文件（pper.inc.php）已经不存在，
   只剩 `pperlist`（状态列表页）。实测：
     * `plugin.php?id=dsu_amupper`        -> 未定义操作
     * `plugin.php?id=dsu_amupper:pper`   -> 模块文件不存在
     * `plugin.php?id=dsu_amupper:pperlist` -> 您需要先登录（模块正常）
   也就是说，卡饭大概率是"登录即自动签到"，插件在用户访问论坛时自己把签到做掉。
   所以流程是：先开首页（触发自动签到），再读状态页确认今天到底签没签；
   状态页显示没签，才走一次标准提交兜底。
"""

from __future__ import annotations

import re

from ..core import Line, NAV_HEADERS, Site, XHR_HEADERS, looks_blocked

BASE = "https://bbs.kafan.cn"
FORUM = f"{BASE}/forum.php"
# 状态列表页（模块存在，游客访问返回"您需要先登录"）
STATUS_PAGE = f"{BASE}/plugin.php?id=dsu_amupper:pperlist"
# 标准提交入口（卡饭这边模块文件缺失，只作兜底尝试）
SUBMIT = f"{BASE}/plugin.php?id=dsu_amupper:pper"

# Discuz 登录后才会渲染这个
NICK = re.compile(r'class="vwmy"[^>]*>([^<]{1,40})<')
FORMHASH = re.compile(r"formhash=([0-9a-fA-F]{8})")
# Discuz 提示块
MESSAGE_BOX = re.compile(r'id="messagetext"[^>]*>(.*?)</div>', re.S)

LOGIN_HINTS = ("尚未登录", "请先登录", "需要登录", "您需要先登录", "登录后才能")
DONE_HINTS = ("已经签到", "已签到", "签到过了", "本日已经签到", "今日已签到", "今天已签到")
OK_HINTS = ("恭喜", "签到成功", "签到完成", "已成功签到", "获得奖励")
# 插件模块缺失 / 未定义，说明这条路走不通，要换状态页
DEAD_END_HINTS = ("模块文件", "插件不存在", "未定义操作")

CONSEC = re.compile(r"连续签到[^0-9]{0,8}(\d+)[^0-9]{0,4}天")
TOTAL = re.compile(r"累计签到[^0-9]{0,8}(\d+)[^0-9]{0,4}天")
CREDIT = re.compile(r"(?:积分|经验)[^0-9]{0,6}(\d+)")


class Kafan(Site):
    key = "KAFAN"
    name = "卡饭论坛"
    fields = ("cookie",)
    doc = (
        "登录 bbs.kafan.cn → F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（要含以 _2132_auth 和 _2132_saltkey 结尾的两条）"
    )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _decode(resp) -> str:
        """卡饭是 GBK 站，显式解码，别指望 requests 一定猜对。"""
        enc = (resp.encoding or "").lower()
        if not enc or enc in ("iso-8859-1", "ascii"):
            enc = "gbk"
        try:
            return resp.content.decode(enc, "ignore")
        except (LookupError, UnicodeDecodeError):
            return resp.text or ""

    @staticmethod
    def _message(html: str) -> str:
        box = MESSAGE_BOX.search(html)
        if not box:
            return ""
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", box.group(1))).strip()

    def _guard(self, resp, html: str, what: str) -> None:
        if looks_blocked(resp.status_code, html, resp.headers):
            raise RuntimeError(f"{what}时被防火墙拦截（HTTP {resp.status_code}），换个网络或代理再试")
        if resp.status_code >= 500:
            raise RuntimeError(f"{what}返回 HTTP {resp.status_code}，站点可能在维护")
        if resp.status_code != 200:
            raise RuntimeError(f"{what}失败 HTTP {resp.status_code}")

    # ------------------------------------------------------------------ #
    def run(self) -> list[Line]:
        session = self.session(headers={**NAV_HEADERS, "Referer": FORUM})

        home = session.get(FORUM, timeout=25)
        html = self._decode(home)
        self._guard(home, html, "打开论坛首页")

        nick = NICK.search(html)
        if not nick:
            if any(hint in html for hint in LOGIN_HINTS):
                raise RuntimeError(
                    "Cookie 已失效（首页提示未登录）。重新登录后复制整段 Cookie，"
                    "要含以 _2132_auth、_2132_saltkey 结尾的那两条"
                )
            raise RuntimeError(
                "Cookie 已失效或不全：首页没有登录态（既没渲染用户名，也没有登录提示）。"
                "重新登录后复制整段 Cookie（F12 → 网络 → 请求标头），"
                "要含以 _2132_auth、_2132_saltkey 结尾的那两条"
            )

        lines: list[Line] = [Line("账号", nick.group(1).strip())]

        # 打开首页这个动作本身就可能触发了插件的自动签到，所以先读状态
        state, message, raw = self._read_status(session)

        if state == "unknown":
            # 状态页没给出结论，再试一次标准提交入口
            state, message, raw = self._submit(session, html)

        if state == "login":
            raise RuntimeError(f"签到被拒：{message} —— Cookie 已失效，重新复制一次")
        if state == "dead":
            raise RuntimeError(
                "卡饭的签到插件结构变了：标准提交模块（dsu_amupper:pper）已不存在，"
                "状态页也读不到签到结果。请手动打开 https://bbs.kafan.cn/plugin.php?id=dsu_amupper:pperlist "
                "确认签到按钮是否还在，然后把看到的情况反馈一下"
            )
        if state == "unknown":
            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()[:160]
            raise RuntimeError(
                "签到结果无法判断（状态页和提交接口都没给出明确结论）。"
                f"页面返回：{snippet or '（空）'} —— 请手动去站点确认"
            )

        lines.append(Line("签到", message or ("今日已签到" if state == "done" else "签到成功")))

        for name, pattern in (("连续签到", CONSEC), ("累计签到", TOTAL)):
            found = pattern.search(raw) or pattern.search(html)
            if found:
                lines.append(Line(name, f"{found.group(1)} 天"))

        if state == "done" and "自动" not in message:
            lines.append(Line("说明", "卡饭的签到是登录即自动完成的，本条为状态确认"))

        return lines

    # ------------------------------------------------------------------ #
    def _read_status(self, session) -> tuple[str, str, str]:
        """读签到状态页，返回 (状态, 提示, 原始文本)。状态：ok/done/login/dead/unknown。"""
        resp = session.get(STATUS_PAGE, timeout=25, headers={"Referer": FORUM})
        text = self._decode(resp)
        self._guard(resp, text, "读取签到状态")

        message = self._message(text)
        body = message or re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))

        if any(hint in body for hint in LOGIN_HINTS):
            return "login", message, text
        if any(hint in body for hint in DONE_HINTS):
            return "done", message or "今日已签到", text
        # 状态页里出现"签到"按钮/链接，说明还没签
        if "还没签到" in text or "未签到" in text or "今天还没有签到" in text:
            return "unknown", "", text
        return "unknown", "", text

    def _submit(self, session, home_html: str) -> tuple[str, str, str]:
        """兜底：走 dsu_amupper 的标准提交入口（卡饭可能已删掉这个模块）。"""
        formhash = FORMHASH.search(home_html)
        if not formhash:
            return "unknown", "", home_html

        params = {
            "id": "dsu_amupper:pper",
            "ppersubmit": "true",
            "formhash": formhash.group(1),
            "infloat": "yes",
            "handlekey": "dsu_amupper",
            "inajax": "1",
            "ajaxtarget": "fwin_content_dsu_amupper",
        }
        resp = session.get(
            SUBMIT,
            params=params,
            timeout=25,
            headers={**XHR_HEADERS, "Referer": FORUM, "X-Requested-With": "XMLHttpRequest"},
        )
        text = self._decode(resp)
        self._guard(resp, text, "提交签到")

        message = self._message(text) or re.sub(
            r"\s+", " ", re.sub(r"<[^>]+>", " ", text)
        ).strip()[:200]

        if any(hint in text for hint in DEAD_END_HINTS):
            return "dead", message, text
        if any(hint in text for hint in LOGIN_HINTS):
            return "login", message, text
        if any(hint in text for hint in DONE_HINTS):
            return "done", message or "今日已签到", text
        if any(hint in text for hint in OK_HINTS):
            return "ok", message or "签到成功", text
        return "unknown", message, text

    # ------------------------------------------------------------------ #
    def verify(self) -> tuple[bool, str]:
        """只读检测：首页能拿到用户名就说明 cookie 有效。"""
        session = self.session(headers={**NAV_HEADERS, "Referer": FORUM})
        try:
            resp = session.get(FORUM, timeout=20)
        except Exception as exc:  # noqa: BLE001
            return False, f"连不上站点：{exc}"
        html = self._decode(resp)
        if looks_blocked(resp.status_code, html, resp.headers):
            return False, f"被防火墙拦截（HTTP {resp.status_code}）"
        if resp.status_code != 200:
            return False, f"检测失败：HTTP {resp.status_code}"
        nick = NICK.search(html)
        if nick:
            return True, f"Cookie 有效：{nick.group(1).strip()}"
        return False, "Cookie 已失效（首页没有登录态）"
