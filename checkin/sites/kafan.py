"""卡饭论坛（bbs.kafan.cn）：Discuz! X + dsu_amupper（打卡机）插件。

这里的结论是靠真实抓包改出来的，和早期的猜测不一样，值得记下来：

1. **编码是 GBK**。响应头写着 `charset=gbk`，但 requests 常按 ISO-8859-1 处理，
   中文关键字就全匹配不上，所以所有解析都用 `_decode()` 显式按 GBK 解。

2. **不是"登录即自动签到"**。插件确实把打卡按钮做成了图片（`dk.png`），
   所以整页没有"签到"两个中文字 —— 早期靠中文关键词判断状态全落空。
   真实流程是导航栏里一个 `<a id="pper_a">`，点了才真的打卡。

3. **提交入口是 `plugin.php?id=dsu_amupper&ppersubmit=true&formhash=xxx`**。
   注意**没有** `:pper` 这个模块名 —— 带 `:pper` 会返回"模块文件不存在"。
   那个链接每次渲染都带一个现生成的 formhash，得从首页现取。

4. **按钮状态由两个元素控制**：
   * `pper_a` 打卡按钮（图片 dk.png）
   * `pper_b` "已完成"标记（图片 wb.png），初始 `style="display:none"`
   签过卡之后服务端**直接不渲染 pper_b**，同时 `pper_a` 的 onclick 里
   不再引用 `pper_b`。所以"有没有 pper_b"就是今天签没签的判据。

5. **提交是幂等的**，重复打卡返回"您已签到完毕，今日已无需再次签到"。
   所以不用先去猜状态，直接提交再看响应，比猜按钮可靠。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from .. import discuz
from ..core import Line, NAV_HEADERS, Site, XHR_HEADERS, looks_blocked

BASE = "https://bbs.kafan.cn"
FORUM = f"{BASE}/forum.php"

# 打卡按钮的链接（带新鲜 formhash），href 里 & 是 &amp; 转义
PPER_LINK = re.compile(r'<a[^>]*id=["\']pper_a["\'][^>]*href=["\']([^"\']+)["\']', re.I)
# "已完成"标记，签过卡之后服务端不再渲染
PPER_DONE_MARK = re.compile(r'id=["\']pper_b["\']', re.I)

# Discuz 提示块
MESSAGE_BOX = re.compile(r'id="messagetext"[^>]*>(.*?)</div>', re.S)
SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)

LOGIN_HINTS = discuz.GUEST_HINTS
DONE_HINTS = ("已经签到", "已签到", "签到完毕", "无需再次签到", "今日已无需", "签到过了")
# 打卡成功的提示形如「由于您累计签到2087次，连续签到0次，特奖励：经验 1」
OK_HINTS = ("累计签到", "连续签到", "特奖励", "恭喜", "签到成功")

CONSEC = re.compile(r"连续签到\s*(\d+)\s*次")
TOTAL = re.compile(r"累计签到\s*(\d+)\s*次")
EXP = re.compile(r"特奖励[：:\s]*(?:经验|威望|金钱)?\s*(\d+)")


class Kafan(Site):
    key = "KAFAN"
    name = "卡饭论坛"
    fields = ("cookie",)
    doc = (
        "登录 bbs.kafan.cn → F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（要含以 _auth 和 _saltkey 结尾的两条）"
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
        """取提示块正文，顺手把内联 script 去掉。

        插件会在提示块里塞一段自动跳转的 JS，不清理的话
        日志里会出现 `if(history.length > ...) document.write(...)` 这种东西。
        """
        box = MESSAGE_BOX.search(html)
        if not box:
            return ""
        raw = SCRIPT_STYLE.sub(" ", box.group(1))
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()

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

        ok, why = discuz.login_verdict(html, "卡饭")
        if not ok:
            raise RuntimeError(why)

        who = discuz.nickname(html)
        lines: list[Line] = [Line("账号", who)] if who else []

        # 打卡地址每次渲染都带新 formhash，优先用页面里的原样链接
        found = PPER_LINK.search(html)
        if found:
            target = urljoin(FORUM, found.group(1).replace("&amp;", "&"))
        else:
            token = discuz.formhash(html)
            if not token:
                raise RuntimeError(
                    "已登录但页面里找不到打卡入口（pper_a）也取不到 formhash。"
                    "卡饭的 dsu_amupper 插件可能被关掉了，请手动打开 "
                    "https://bbs.kafan.cn/forum.php 确认导航栏还有没有打卡图标"
                )
            target = f"{BASE}/plugin.php?id=dsu_amupper&ppersubmit=true&formhash={token}"

        # 首页里有没有 pper_b 只是参考，真正结论以提交后的响应为准（提交是幂等的）
        already = not PPER_DONE_MARK.search(html)

        resp = session.get(
            target,
            timeout=25,
            headers={**XHR_HEADERS, "Referer": FORUM, "X-Requested-With": "XMLHttpRequest"},
        )
        text = self._decode(resp)
        self._guard(resp, text, "提交打卡")

        message = self._message(text)
        body = message or SCRIPT_STYLE.sub(" ", text)

        if any(h in body for h in LOGIN_HINTS):
            raise RuntimeError(f"打卡被拒：{message or '需要登录'} —— {discuz.cookie_hint('卡饭')}")

        state = "unknown"
        if any(h in body for h in DONE_HINTS):
            state = "done"
        elif any(h in body for h in OK_HINTS):
            state = "ok"

        if state == "unknown":
            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", SCRIPT_STYLE.sub(" ", text)))
            raise RuntimeError(
                "打卡结果无法判断（既没看到成功也没看到已签到）。"
                f"页面返回：{snippet[:160] or '（空）'} —— 请手动去站点确认"
            )

        if state == "done":
            lines.append(Line("签到", message or "今日已签到"))
        else:
            lines.append(Line("签到", message or "签到成功"))

        # 打卡成功的提示里就带着天数，直接解析，不用再请求状态页
        for name, pattern, unit in (("连续签到", CONSEC, "次"), ("累计签到", TOTAL, "次")):
            hit = pattern.search(body)
            if hit:
                lines.append(Line(name, f"{hit.group(1)} {unit}"))
        exp = EXP.search(body)
        if exp:
            lines.append(Line("奖励", f"经验 +{exp.group(1)}"))

        if already and state == "done":
            lines.append(Line("说明", "首页显示今天已经打过卡了，本次只做确认"))
        return lines

    # ------------------------------------------------------------------ #
    def verify(self) -> tuple[bool, str]:
        """只读检测：首页有退出链接就说明 cookie 有效。"""
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
        return discuz.login_verdict(html, "卡饭")
