"""品技论坛（www.tekqart.com）：Discuz! X + zqlj_sign「天天打卡」插件。

和飞牛社区（FNNASCLUB）用的是同一个插件，但这站有三个额外的坑，都是实测出来的：

1. **挂在 Cloudflare 后面**。机房 / 海外出口会被 `403 + Just a moment` 拦（响应头
   `cf-mitigated: challenge`），普通 requests 过不去。所以走 `browser_request()`
   换 TLS 指纹，并且后续提交**复用同一个 session** —— 换 session 就等于换了会话，
   刚取到的 sign 立刻作废。浏览器里换来的 `cf_clearance` 和 IP + UA 绑定，默认丢掉；
   确实要复用就配 `keep_cf=1`，同时把当时的浏览器 UA 填到 `ua` 字段。

2. **游客态也照样渲染「点击打卡」按钮**。实测用游客会话 GET
   `plugin.php?id=zqlj_sign&sign=<8位hex>`，服务端只是原样返回同一个打卡页，
   既不给错误也不打卡。所以「有没有按钮」「有没有报错」都不能当登录判据。
   可靠判据是页面头部的 `discuz_uid = '0'`（游客）以及 Discuz 的退出链接。

3. **sign 是一次性 8 位 hex，每次渲染都变**，必须先拉签到页现取再 GET；
   按钮在 `<div class="bm signbtn cl">` 里的 `<a class="btna">`。
   另外榜单里每个用户的「今日状态」列都写着**「今日已打卡」**，
   所以拿这个短语做全文匹配会把任何一次响应都误判成「已打卡」——
   只能认按钮文案或 `messagetext` 提示块里的完整句子。

页面是 UTF-8（响应头明确写了 `charset=utf-8`），cookie 前缀 `XnMp_2132_`。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from .. import discuz
from ..core import Line, NAV_HEADERS, Site, looks_blocked

BASE = "https://www.tekqart.com"
SIGN_PAGE = f"{BASE}/plugin.php?id=zqlj_sign"

# 打卡按钮所在容器；取不到时再退到全页找带 sign= 的链接
SIGN_BOX = re.compile(
    r'<div[^>]*class=["\'][^"\']*signbtn[^"\']*["\'][^>]*>(.*?)</div>', re.S | re.I
)
SIGN_LINK = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)
SIGN_PARAM = re.compile(r"sign=([0-9a-fA-F]{4,64})")

# Discuz 每个页面都会写这行：游客是 '0'，登录后是当前 uid
DISCUZ_UID = re.compile(r"discuz_uid\s*=\s*['\"](\d+)['\"]")

# 页面骨架特征：用来区分「真的没登录」和「拿到的是维护页 / 验证页 / WAF 页」
PAGE_MARKERS = ("zqlj_sign", "天天打卡", "signbtn", "打卡日历")

# 站点偶尔会对新会话出算术访问验证（Discuz secqaa）。这里只识别并给可照做的提示，
# 不自动提交答案 —— 算式形态不固定，猜错反而会触发更多风控。
SECQAA = re.compile(
    r'name=["\']answer["\']|secqsubmit|font-size:\s*16px[^>]*>[^<]*=\s*\?', re.I
)

MESSAGE_BOX = re.compile(r'id="messagetext"[^>]*>(.*?)</div>', re.S | re.I)
SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
# Discuz showmessage 会在提示后面拼一句自动跳转说明，不是签到结果，别混进通知
JUMP_TAIL = re.compile(r"如果您的浏览器没有自动跳转.*$", re.S)

# 提交响应里表示「要登录」的明确说法。这里刻意不用 discuz.GUEST_HINTS：
# 它含「立即注册」「先登录」这类游客页导航词，登录后的提示页也可能带着，
# 拿来做全文匹配会把一次成功的打卡误报成「Cookie 失效」。
LOGIN_HINTS = ("请先登录", "尚未登录", "您需要先登录", "登录后才能", "请先登陆")
# 必须是完整句子，不能用「今日已打卡」——榜单里到处都是这四个字
OK_HINTS = ("恭喜您，打卡成功", "恭喜您,打卡成功", "打卡成功！")
DONE_HINTS = ("您今天已经打过卡了", "请勿重复操作", "今天已经打过卡")

# 个人打卡动态：登录后签到页侧栏会渲染这些 li
STAT_KEYS = (
    "最近打卡", "本月打卡", "连续打卡", "累计打卡",
    "累计奖励", "最近奖励", "当前打卡等级",
)
STAT_LI = re.compile(r"<li[^>]*>\s*([^<：:]{2,12})[：:]\s*([^<]{1,40}?)\s*</li>", re.S)
REWARD = re.compile(r"(\d+)\s*经验值")


class Tekqart(Site):
    key = "TEKQART"
    name = "品技论坛"
    fields = ("cookie", "keep_cf", "ua")
    doc = (
        "登录 www.tekqart.com → F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（必须含 XnMp_2132_auth 和 XnMp_2132_saltkey）。站点挂在 Cloudflare 后面，"
        "默认会丢掉浏览器里的 cf_clearance；若 NAS 出口仍被挑战，设 keep_cf=1 "
        "并把浏览器 UA 一起填到 ua"
    )
    cf_impersonates = ("safari15_5", "chrome120", "chrome110")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _text(resp) -> str:
        """页面明确是 UTF-8；优先按 content 解，桩测试没有 content 时退回 text。"""
        content = getattr(resp, "content", None)
        if content:
            try:
                return content.decode("utf-8", "ignore")
            except (AttributeError, UnicodeDecodeError):
                pass
        return str(getattr(resp, "text", "") or "")

    @staticmethod
    def _message(html: str) -> str:
        box = MESSAGE_BOX.search(html or "")
        if not box:
            return ""
        clean = SCRIPT_STYLE.sub(" ", box.group(1))
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", clean)).strip()
        return JUMP_TAIL.sub("", plain).strip()

    @staticmethod
    def _plain(html: str) -> str:
        clean = SCRIPT_STYLE.sub(" ", html or "")
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", clean)).strip()

    def _guard(self, resp, html: str, what: str, *, need_page: bool = True) -> None:
        if looks_blocked(resp.status_code, html, getattr(resp, "headers", None)):
            raise RuntimeError(f"{what}时被 Cloudflare 拦截（HTTP {resp.status_code}），换个出口或配代理再试")
        if SECQAA.search(html or ""):
            raise RuntimeError(
                f"{what}时站点要求先回答一次算术访问验证。用浏览器打开 {BASE}/ "
                "手动答一次、重新复制 Cookie 再跑；验证和出口 IP 绑定，换了网络可能要再答一次"
            )
        if resp.status_code >= 500:
            raise RuntimeError(f"{what}返回 HTTP {resp.status_code}，站点可能正在维护")
        if resp.status_code != 200:
            raise RuntimeError(f"{what}失败 HTTP {resp.status_code}")
        if need_page and not any(marker in html for marker in PAGE_MARKERS):
            snippet = self._plain(html)[:120]
            raise RuntimeError(
                f"{what}拿到的不是打卡页（{snippet or '空响应'}），站点可能改版或临时维护"
            )

    @staticmethod
    def _login_state(html: str) -> tuple[bool, str]:
        """返回 (是否已登录, uid)。游客页 discuz_uid = '0'。

        **不能调 discuz.logged_in()**：它会拿 `nickname()` 当兜底，而这个签到页
        主体就是一张 30 人的打卡榜单，全是 `space-uid-xxx.html` 用户名链接，
        `NICK_PATTERNS` 最后一条会把榜单第一个人当成当前用户，游客也被判成已登录。
        这里只认两样不会被榜单伪造的东西：`discuz_uid`、退出链接。
        """
        found = DISCUZ_UID.search(html)
        if found:
            return found.group(1) != "0", found.group(1)
        return bool(discuz.LOGOUT.search(html)), ""

    @staticmethod
    def _who(html: str) -> str:
        """用户名只从导航栏的 vwmy 元素取。

        同样不能用 discuz.nickname() 的全套兜底 —— 它会抓到榜单里的第一个用户名。
        """
        for pattern in discuz.NICK_PATTERNS[:2]:
            found = pattern.search(html or "")
            if not found:
                continue
            name = re.sub(r"<[^>]+>", "", found.group(1)).strip()
            bad = {"登录", "注册", "退出", "设置", "消息", "提醒", "游客", "匿名"}
            if name and len(name) <= 40 and name.lower() not in bad:
                return name
        return ""

    @classmethod
    def _sign_button(cls, html: str) -> tuple[str, str]:
        """返回 (带 sign 的链接, 按钮文案)。优先只在 signbtn 容器里找。"""
        sources = list(SIGN_BOX.findall(html)) + [html]
        for source in sources:
            for link in SIGN_LINK.finditer(source):
                href, label = link.group(1), re.sub(r"<[^>]+>", "", link.group(2)).strip()
                if not SIGN_PARAM.search(href):
                    continue
                return href.replace("&amp;", "&"), label
        return "", ""

    @staticmethod
    def _stats_lines(html: str) -> list[Line]:
        """读个人打卡动态，纯展示。

        侧栏的「打卡统计」是全站数据（今日打卡 4912 人、累计打卡 4424820 人次），
        和「累计打卡」「累计奖励」这两个字段名重名，直接全页扫 li 会把全站数字
        当成自己的成绩。所以：
        * 有「我的打卡动态」区块 → 只在那一小段里解析；
        * 没有 → 只认个人专属字段，避开和全站统计重名的「累计打卡 / 累计奖励」。
        """
        personal = ("最近打卡", "本月打卡", "连续打卡", "当前打卡等级", "最近奖励")
        idx = (html or "").find("我的打卡动态")
        if idx >= 0:
            source, keys = html[idx : idx + 1500], STAT_KEYS
        else:
            source, keys = html or "", personal

        out: list[Line] = []
        seen: set[str] = set()
        for found in STAT_LI.finditer(source):
            key, value = found.group(1).strip(), found.group(2).strip()
            if key not in keys or key in seen or not value:
                continue
            seen.add(key)
            out.append(Line(key, value))
            if len(out) >= 7:
                break
        return out

    @classmethod
    def _classify(cls, text: str, button_label: str) -> tuple[str, str]:
        """返回 (状态, 提示)。状态：ok / done / login / noop / unknown。"""
        message = cls._message(text)
        if any(hint in text for hint in LOGIN_HINTS):
            return "login", message or "需要登录"
        if any(hint in text for hint in OK_HINTS):
            return "ok", message or "打卡成功"
        if any(hint in text for hint in DONE_HINTS):
            return "done", message or "今日已打卡"
        # 按钮本身就写着「今日已打卡」：说明插件没给新的打卡入口
        if "已打卡" in button_label:
            return "done", "今日已打卡"
        # 响应还是打卡页本身、没有提示块 = 什么都没发生（sign 失效 / 会话不对）
        if "signbtn" in text or "点击打卡" in text:
            return "noop", ""
        return "unknown", message or cls._plain(text)[:160]

    # ------------------------------------------------------------------ #
    def _open(self):
        """打开打卡页，返回 (响应文本, 复用用的 session, 按钮链接, 按钮文案)。"""
        cookie_str, kept_cf = self.cf_cookies()
        headers = self.cf_headers({**NAV_HEADERS, "Referer": f"{BASE}/"}, kept_cf)
        resp, session = self.browser_request(
            lambda s: s.get(SIGN_PAGE, timeout=25),
            cookie=cookie_str,
            headers=headers,
        )
        html = self._text(resp)
        self._guard(resp, html, "打开打卡页")
        return html, session

    def run(self) -> list[Line]:
        html, session = self._open()

        logged, uid = self._login_state(html)
        if not logged:
            raise RuntimeError(
                "Cookie 已失效或不全：打卡页是游客态（discuz_uid=0）。"
                + discuz.cookie_hint("品技论坛")
            )

        who = self._who(html)
        lines: list[Line] = [
            Line("账号", who or (f"已登录（uid {uid}）" if uid else "已登录"))
        ]

        href, label = self._sign_button(html)
        if not href:
            # 只有页面自己明确说「今天已经打过卡」才能判 done。
            # 「我的打卡动态」区块和榜单里的「今日已打卡」都不是本人的打卡状态。
            if any(hint in html for hint in DONE_HINTS):
                lines.append(Line("签到", "今日已打卡"))
                return lines + self._stats_lines(html)
            raise RuntimeError(
                "已登录但打卡页里找不到「点击打卡」按钮。插件可能改版或被关闭，"
                f"请手动打开 {SIGN_PAGE} 确认按钮还在"
            )

        if "已打卡" in label:
            # 插件已经不给新入口了，不必再发一次 GET（提交本身幂等，但省一次请求）
            lines.append(Line("签到", "今日已打卡"))
            return lines + self._stats_lines(html)

        target = urljoin(SIGN_PAGE, href)
        resp = session.get(
            target,
            timeout=25,
            headers={**NAV_HEADERS, "Referer": SIGN_PAGE, "Sec-Fetch-Site": "same-origin"},
        )
        text = self._text(resp)
        # 提交后是 Discuz 的提示页，不一定带打卡页骨架，所以不做页面特征检查
        self._guard(resp, text, "提交打卡", need_page=False)

        state, message = self._classify(text, label)
        if state == "login":
            raise RuntimeError(
                f"打卡被拒：{message} —— {discuz.cookie_hint('品技论坛')}"
            )
        if state == "noop":
            raise RuntimeError(
                "打卡没有生效：按钮点了之后服务端原样返回打卡页，没给任何结果提示。"
                "通常是 sign 已过期或 Cloudflare 会话中途变了，隔一两分钟重跑一次；"
                "反复如此就把 Cookie 重新复制一次"
            )
        if state == "unknown":
            raise RuntimeError(
                "打卡结果无法判断（既没看到成功，也没看到已打卡）。"
                f"页面返回：{message or '（空）'} —— 请手动去站点确认"
            )

        lines.append(Line("签到", message or ("今日已打卡" if state == "done" else "打卡成功")))
        reward = REWARD.search(message)
        if reward:
            lines.append(Line("奖励", f"{reward.group(1)} 经验值"))

        # 打卡后再拉一次签到页读个人动态（顺带确认登录态还在）
        try:
            detail = session.get(SIGN_PAGE, timeout=20, headers={"Referer": SIGN_PAGE})
            lines.extend(self._stats_lines(self._text(detail)))
        except Exception:  # noqa: BLE001 - 统计读不到不影响签到结论
            pass
        return lines

    # ------------------------------------------------------------------ #
    def verify(self) -> tuple[bool, str]:
        """只读检测：打开打卡页看 discuz_uid / 退出链接，不点打卡。"""
        cookie_str, kept_cf = self.cf_cookies()
        headers = self.cf_headers({**NAV_HEADERS, "Referer": f"{BASE}/"}, kept_cf)
        try:
            resp, _ = self.browser_request(
                lambda s: s.get(SIGN_PAGE, timeout=20),
                cookie=cookie_str,
                headers=headers,
            )
        except RuntimeError as exc:
            # browser_request 抛的是 Cloudflare 排查指引，原样给用户看
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"连不上站点：{exc}"

        html = self._text(resp)
        if looks_blocked(resp.status_code, html, getattr(resp, "headers", None)):
            return False, f"被防火墙拦截（HTTP {resp.status_code}）"
        if resp.status_code != 200:
            return False, f"检测失败：HTTP {resp.status_code}"
        if not any(marker in html for marker in PAGE_MARKERS):
            return False, "检测失败：拿到的不是打卡页，站点可能改版或正在维护"

        logged, uid = self._login_state(html)
        if logged:
            who = self._who(html) or (f"uid {uid}" if uid else "")
            return True, f"Cookie 有效：{who}" if who else "Cookie 有效（已登录）"
        return False, f"Cookie 已失效（打卡页是游客态）。{discuz.cookie_hint('品技论坛')}"
