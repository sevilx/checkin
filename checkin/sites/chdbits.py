"""CHDBits（ptchdbits.co）：NexusPHP 答题式签到（BAKA & TEST SUMMON）。

入口不是 attendance.php（404），而是 bakatest.php。GET 拿到一张随机常识题表单，
里面有 ``questionid`` 和三个提交按钮：提交 / 仅可换一题 / 不会（wantskip）。

站点规则原文的关键几条：
1) 点「不会」得 5 魔力，照样算签到；
2) 已退学用户签到得 10 魔力，其他等级随机得「连续天数 ~ 连续天数×2」；
3) 不会却答对额外 +5；答错扣 1（换题情况下扣更多）。

随机常识题没法用纯 HTTP 可靠作答，预置答案只会引入答错扣分的风险。
所以本模块**固定走 wantskip（不会）**：收益略低但零风险、零维护，不答题、
不预置任何答案。流程是 GET bakatest.php 现取 questionid（题目每次都随机）
并确认登录态，再 POST ``questionid=<id>&choice[]=<n>&wantskip=不会``，
Referer 指向答题页。

**为什么必须带 choice[]**：NexusPHP 的 bakatest.php 在验证分支里先算
``$answersum``，没选 radio 就直接 ``stderr("出错了", "没有选择选项哦...")``，
哪怕点了「不会」也一样。stderr 用默认标题渲染，返回的就是首页导航那张页面，
看起来像「提交没生效」。因为 wantskip 分支会跳过答错扣分逻辑，
随手带一个当次页面上的合法选项即可：零扣分风险，答对了还可能额外 +5。
选项值每次从 GET 到的表单里现取，不写死答案。

登录票据是标准 NexusPHP 的 c_secure_* 系列，判据用 c_secure_uid + c_secure_pass。
"""

from __future__ import annotations

import re

from ..core import Line, NAV_HEADERS, XHR_HEADERS, Site, looks_blocked, parse_cookie

BASE = "https://ptchdbits.co"
SIGN_PAGE = f"{BASE}/bakatest.php"

# <input type="hidden" name="questionid" value="320"> —— 单双引号、属性顺序都兼容
QUESTION_ID = re.compile(
    r"""name\s*=\s*['"]questionid['"][^>]*?value\s*=\s*['"](\d+)['"]"""
    r"""|value\s*=\s*['"](\d+)['"][^>]*?name\s*=\s*['"]questionid['"]""",
    re.I,
)
LOGIN_HINTS = ("请先登录", "尚未登录", "您需要登录", "登录后才能", "请登录", "需要登录")
# 已签到要用完整句，避免把答题页上的规则说明 / 统计数字误判成已签到
# NexusPHP 原文是「今天已经签过到了(已连续N天签到)」，它没有「今天已经签到」这个子串，
# 所以把「签过到」也收进来，否则重复签到会被误判成「结果无法判断」。
DONE_HINTS = (
    "今天已经签到", "今日已签到", "您已签到", "已经签到过", "重复签到",
    "今天已签", "今日已签", "请勿重复签到", "签过到",
)
# <input type='radio' name='choice[]' value='4'> —— 属性顺序兼容，取非 0 的合法选项值
CHOICE_VALUE = re.compile(
    r"""<input\b[^>]*name\s*=\s*['"]choice\[\]['"][^>]*?value\s*=\s*['"](\d+)['"]"""
    r"""|<input\b[^>]*value\s*=\s*['"](\d+)['"][^>]*?name\s*=\s*['"]choice\[\]['"]""",
    re.I,
)
USERCOMMENT = re.compile(
    r"""<textarea\b[^>]*name\s*=\s*['"]usercomment['"][^>]*>([^<]*)</textarea\s*>""",
    re.I | re.S,
)
# 成功文案分两级，区别在于「会不会出现在答题页的规则说明区」。
#
# * EXPLICIT_OK_HINTS（排他播报）：直接的成功结果播报，不会出现在静态规则说明里。
#   命中即成功，不受 new_form 约束 —— 保留「真实签到成功时站点仍回新题表单+顶部提示」的可能。
# * SOFT_OK_HINTS（软文案）：规则区常见「答对可连续签到翻倍」「获得 5 魔力值」这类话，
#   单词命中不足以证明成功，必须叠加「没返回新题表单」这个前提才算数。
# 刻意不把单独的「魔力」列进来 —— 答题页规则说明里就写着「获得 5 魔力值」。
EXPLICIT_OK_HINTS = (
    "签到成功", "签到完成", "恭喜", "签到奖励",
)
SOFT_OK_HINTS = (
    "连续签到", "已连续签到", "魔力增加", "魔力值增加",
    "本次获得", "本次增加",
)
CONSEC = re.compile(r"连[续签]{0,2}[^0-9]{0,8}(\d+)[^0-9]{0,4}天")
MAGIC = re.compile(
    r"(?:获得|增加|奖励|加上)[^0-9]{0,8}(\d+)\s*(?:点)?\s*魔力"
    r"|(\d+)\s*(?:点)?\s*魔力(?:值)?[^。；;]{0,6}(?:增加|获得|奖励)"
)


class CHDBits(Site):
    key = "CHDBITS"
    name = "CHDBits"
    fields = ("cookie",)
    doc = (
        "登录 ptchdbits.co → F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（要含 c_secure_uid 和 c_secure_pass）。站点是 NexusPHP 答题签到，"
        "题目每次随机、纯 HTTP 无法可靠作答，程序固定点「不会」稳妥拿 5 魔力，"
        "不答题、不预置答案，也绝不会答错扣分"
    )

    def __init__(self, account: dict, index: int = 0):
        super().__init__(account, index)
        cookies = parse_cookie(self.cookie)
        if not (cookies.get("c_secure_uid") and cookies.get("c_secure_pass")):
            raise ValueError(
                "Cookie 里缺少 c_secure_uid / c_secure_pass（NexusPHP 登录票据），"
                "请重新登录 ptchdbits.co 后从「F12 → 网络 → 请求标头 → Cookie」整行复制"
            )

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _text(resp) -> str:
        """站点是 UTF-8；优先按 content 解，桩测试没有 content 时退回 text。"""
        content = getattr(resp, "content", None)
        if content:
            try:
                return content.decode("utf-8", "ignore")
            except (AttributeError, UnicodeDecodeError):
                pass
        return str(getattr(resp, "text", "") or "")

    @staticmethod
    def _plain(html: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()

    @staticmethod
    def _question_id(html: str) -> str:
        for match in QUESTION_ID.finditer(html or ""):
            qid = (match.group(1) or match.group(2) or "").strip()
            if qid.isdigit():
                return qid
        return ""

    @staticmethod
    def _looks_login(html: str) -> bool:
        lowered = (html or "").lower()
        # 答题页一定带 questionid；有登录表单又没有 questionid 才算游客态
        if "questionid" in lowered:
            return False
        return any(
            token in lowered
            for token in ('name="username"', "name='username'", 'name="password"', "name='password'")
        )

    @staticmethod
    def _choice_value(html: str) -> str:
        """从本次 GET 到的表单里取一个非 0 的 radio 值。

        站点在验证分支里用 ``array_sum($_POST["choice"])`` 判空，值为 0 会被当成
        「没选选项」直接 stderr；所以必须挑一个 > 0 的。wantskip 分支不会因答错扣分。
        """
        for match in CHOICE_VALUE.finditer(html or ""):
            value = (match.group(1) or match.group(2) or "").strip()
            if value.isdigit() and int(value) > 0:
                return value
        return ""

    @staticmethod
    def _usercomment(html: str) -> str:
        """取页面上「此刻心情」的预填值，让 POST 和浏览器表单提交一致（非必需，仅日志用）。"""
        match = USERCOMMENT.search(html or "")
        return match.group(1).strip() if match else ""

    @staticmethod
    def _gain(text: str) -> str:
        for match in MAGIC.finditer(text or ""):
            num = (match.group(1) or match.group(2) or "").strip()
            if num.isdigit() and int(num) > 0:
                return num
        return ""

    def _guard(self, resp, html: str, what: str) -> None:
        if looks_blocked(resp.status_code, html, getattr(resp, "headers", None)):
            raise RuntimeError(
                f"{what}时被防火墙拦截（HTTP {resp.status_code}），换个网络或给这个站点配代理再试"
            )
        if resp.status_code >= 500:
            raise RuntimeError(f"{what}返回 HTTP {resp.status_code}，站点可能正在维护")
        if resp.status_code != 200:
            raise RuntimeError(f"{what}失败 HTTP {resp.status_code}")

    # ------------------------------------------------------------------ #
    def run(self) -> list[Line]:
        session = self.session(headers={**NAV_HEADERS, "Referer": f"{BASE}/"})

        # 1) GET 现取 questionid —— 题目随机，绝不能缓存或预置答案
        resp = session.get(SIGN_PAGE, timeout=25)
        html = self._text(resp)
        # 今天已签到时服务端可能直接给一张无表单的提示页，这里不强制页面骨架，
        # 后面按 questionid / 已签到文案分别处理。
        self._guard(resp, html, "打开答题签到页")

        url = str(getattr(resp, "url", "") or "").lower()
        if "login.php" in url or self._looks_login(html):
            raise RuntimeError(
                "Cookie 已失效：答题页要求登录。"
                "请重新登录 ptchdbits.co 后整行复制 Cookie（要含 c_secure_uid / c_secure_pass）"
            )

        qid = self._question_id(html)
        if not qid:
            plain = self._plain(html)
            if any(hint in plain for hint in DONE_HINTS):
                return [Line("签到", "今日已签到")]
            raise RuntimeError(
                "答题页里找不到 questionid（它由服务端随机生成）。"
                f"页面返回：{plain[:160] or '空'} —— 如果今天还没签到，请手动打开 {SIGN_PAGE} 确认页面结构"
            )

        # 站点即使点「不会」也要求选中一个 radio：没有 choice[] 会直接 stderr 出
        # 「没有选择选项哦，就算不会也要随便选择一个选项哦」的默认标题页（看起来像首页导航）。
        choice = self._choice_value(html)
        if not choice:
            raise RuntimeError(
                "答题页里找不到可提交的 choice[] 选项值，无法走「不会」通道。"
                f"请手动打开 {SIGN_PAGE} 确认表单结构是否又改版了"
            )

        lines: list[Line] = [Line("签到方式", "不会（稳妥 +5 魔力，不答题）")]

        # 2) POST wantskip（"不会"）：拿 5 魔力、算签到，且绝不会答错扣分。
        # choice[] 只带一个当前题目上的合法值（wantskip 分支不判错扣分）。
        post = session.post(
            SIGN_PAGE,
            data={
                "questionid": qid,
                "choice[]": choice,
                "usercomment": self._usercomment(html),
                "wantskip": "不会",
            },
            headers={
                **XHR_HEADERS,
                "Referer": SIGN_PAGE,
                "Origin": BASE,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=25,
        )
        body = self._text(post)
        self._guard(post, body, "提交「不会」签到")

        post_url = str(getattr(post, "url", "") or "").lower()
        result = self._plain(body)
        if "login.php" in post_url or self._looks_login(body):
            raise RuntimeError("提交签到时登录态失效（被导向登录页），Cookie 已失效，重新复制一次")
        if any(hint in result for hint in LOGIN_HINTS):
            raise RuntimeError(f"签到被拒：{result[:160] or '需要登录'} —— Cookie 已失效，重新复制一次")

        if any(hint in result for hint in DONE_HINTS):
            lines.append(Line("签到", "今日已签到"))
            return lines

        new_form = bool(self._question_id(body))
        gain = self._gain(result)
        # 两种成功证据，强度不同：
        # 1) 排他播报（签到成功 / 恭喜 / 签到奖励…）不会出现在规则说明里，命中即成功 ——
        #    即使站点成功后再渲染一张新题，也不能把它误判成「没生效」；
        # 2) 软文案（连续签到…）和「获得 N 魔力」数字型提示，规则说明区里也有，
        #    只有在没返回新题表单时才算成功，否则就是换题页上的规则说明。
        # 都取不到时按保守处理：宁可报「没生效」让用户手动确认，也不谎报成功导致漏签。
        explicit_ok = any(hint in result for hint in EXPLICIT_OK_HINTS)
        soft_ok = (any(hint in result for hint in SOFT_OK_HINTS) or bool(gain)) and not new_form
        strong_ok = explicit_ok or soft_ok
        if new_form and not strong_ok:
            raise RuntimeError(
                "签到没生效：提交「不会」后站点又返回了一张新题，没有给出明确的签到成功提示。"
                f"页面返回：{result[:160] or '空'} —— 请手动打开 {SIGN_PAGE} 确认今天是否真的签到成功"
            )
        if not strong_ok:
            raise RuntimeError(
                f"签到结果无法判断（既没看到魔力增加，也没看到已签到）：{result[:160] or '空响应'} —— "
                f"请手动打开 {SIGN_PAGE} 确认，站点提示文案可能改版了"
            )

        lines.append(Line("签到", "签到成功（已选「不会」，稳妥获得魔力）"))
        consec = CONSEC.search(result)
        if consec:
            lines.append(Line("连续签到", f"{consec.group(1)} 天"))
        if gain:
            lines.append(Line("本次魔力", gain))
        return lines

    # ------------------------------------------------------------------ #
    def verify(self) -> tuple[bool, str]:
        """只读检测：能取到随机题目就算登录态有效；今天已签也算有效。不提交任何表单。"""
        session = self.session(headers={**NAV_HEADERS, "Referer": f"{BASE}/"})
        try:
            resp = session.get(SIGN_PAGE, timeout=20)
        except Exception as exc:  # noqa: BLE001
            return False, f"连不上站点：{exc}"

        html = self._text(resp)
        if looks_blocked(resp.status_code, html, getattr(resp, "headers", None)):
            return False, f"被防火墙拦截（HTTP {resp.status_code}）"
        if resp.status_code != 200:
            return False, f"检测失败：HTTP {resp.status_code}"

        url = str(getattr(resp, "url", "") or "").lower()
        if "login.php" in url or self._looks_login(html):
            return False, "Cookie 已失效（答题页要求登录）"
        if self._question_id(html):
            return True, "Cookie 有效（能取到今日题目）"
        plain = self._plain(html)
        if any(hint in plain for hint in DONE_HINTS):
            return True, "Cookie 有效（今天已签到）"
        return False, f"检测失败：答题页既没有题目也没有已签到提示（{plain[:120] or '空'}）"
