"""Discuz! 论坛的通用登录态判断。

四个 Discuz 站（福利吧 / 吾爱破解 / 卡饭 / 恩山）模板各不相同，踩过的坑：

1. **不能用 `class="vwmy"` 当唯一判据**。
   * 卡饭的模板根本不渲染这个元素（有"设置/消息/提醒/退出"整排菜单，就是没有用户名）
   * 吾爱同样没有
   * 福利吧虽然有，但用户名是**子元素** `<a>`：
     `<strong class="vwmy"><a href="space-uid-6978.html">xfgb</a></strong>`，
     正则 `class="vwmy"[^>]*>([^<]+)<` 要求紧跟文本，会匹配失败
   把 vwmy 当判据，就会把完全有效的 cookie 判成"已失效"。

2. **真正通用的是"退出链接"**：`member.php?mod=logging&action=logout`
   只有登录后才会渲染（游客页没有），四个站全都有。而且它带的 formhash
   就是当前会话的正确值，可以顺手拿来提交。

3. **cookie 前缀每个站都不同**（Discuz 的 `cookiepre` 配置项）：
   福利吧 `S5r8_2132_*`、吾爱 `htVC_2132_*`、卡饭 `r6pb_69df_*`、恩山 `rHEX_2132_*`。
   所以给用户的提示不能写死 `_2132_auth`，那样会让人照着找一个根本不存在的名字。
"""

from __future__ import annotations

import re

# 退出链接：登录后才有，四个站通用。&amp; 和 & 两种转义都要认
LOGOUT = re.compile(
    r"member\.php\?mod=logging(?:&amp;|&)action=logout",
    re.I,
)

# formhash 的几种出现位置，按可靠性排序
FORMHASH_PATTERNS = (
    # 退出链接里带的，最可靠
    re.compile(r"member\.php\?mod=logging(?:&amp;|&)action=logout(?:&amp;|&)formhash=([0-9a-zA-Z]{8})"),
    # 表单隐藏域
    re.compile(r'name=["\']formhash["\'][^>]*?value=["\']([0-9a-zA-Z]{8})["\']'),
    re.compile(r'value=["\']([0-9a-zA-Z]{8})["\'][^>]*?name=["\']formhash["\']'),
    # 内联 JS
    re.compile(r"formhash\s*[:=]\s*['\"]([0-9a-zA-Z]{8})['\"]"),
    # 兜底：任意 formhash=xxx
    re.compile(r"formhash=(?:&amp;)?([0-9a-zA-Z]{8})"),
)

# 用户名的几种渲染方式，按可靠性排序。都要容忍子元素
NICK_PATTERNS = (
    # 福利吧：<strong class="vwmy"><a ...>xfgb</a></strong>
    re.compile(r'<strong[^>]*class=["\'][^"\']*vwmy[^"\']*["\'][^>]*>\s*(?:<a[^>]*>\s*)?([^<\s][^<]{0,39}?)\s*<', re.I),
    # 其他模板：<a class="vwmy" ...>名字</a>
    re.compile(r'class=["\'][^"\']*vwmy[^"\']*["\'][^>]*>\s*(?:<a[^>]*>\s*)?([^<\s][^<]{0,39}?)\s*<', re.I),
    # 兜底：id="um" 区块里的第一个 <a>
    re.compile(r'id=["\']um["\'][\s\S]{0,600}?<a[^>]*>([^<\s][^<]{0,39}?)</a>', re.I),
    # 再兜底：空间链接里的名字
    re.compile(r'href=["\'][^"\']*space-uid-\d+[^"\']*["\'][^>]*>([^<\s][^<]{0,39}?)</a>', re.I),
)

# 明确的游客提示。命中就说明 cookie 确实没带登录票据
GUEST_HINTS = (
    "尚未登录",
    "请先登录",
    "您需要先登录",
    "需要登录",
    "登录后才能",
    "先登录",
    "立即注册",
)

# 明显的失败占位，不能当用户名
_BAD_NICKS = {
    "登录", "注册", "退出", "设置", "消息", "提醒", "积分", "用户", "会员",
    "游客", "匿名", "login", "logout", "register",
}


def formhash(html: str) -> str:
    """从页面里抠出 formhash，抠不到返回空串。"""
    for pattern in FORMHASH_PATTERNS:
        found = pattern.search(html)
        if found:
            return found.group(1)
    return ""


def nickname(html: str) -> str:
    """尽量取到用户名。取不到返回空串 —— 调用方不该因为取不到就判失败。"""
    for pattern in NICK_PATTERNS:
        for found in pattern.finditer(html):
            name = re.sub(r"<[^>]+>", "", found.group(1)).strip()
            if not name or len(name) > 40:
                continue
            if name.lower() in _BAD_NICKS:
                continue
            return name
    return ""


def logged_in(html: str) -> bool:
    """判登录：有退出链接就算登录。

    比 vwmy 稳，因为退出链接是 Discuz 各模板都会渲染的（它是导航栏固定项），
    而用户名区块经常被模板作者裁掉或改结构。
    """
    if LOGOUT.search(html):
        return True
    # 兜底：万一某个模板把退出链接也裁了但还留着用户名，也认 ——
    # 前提是没有游客提示，避免把登录框里的示例文字当成登录态
    if not is_guest(html) and nickname(html):
        return True
    return False


def is_guest(html: str) -> bool:
    """页面是否有明确的"请先登录"之类提示。"""
    return any(hint in html for hint in GUEST_HINTS)


def cookie_hint(site_name: str = "") -> str:
    """拼一句不误导人的 cookie 提示。

    不能写死 `_2132_auth`：每个站的前缀都不一样（Discuz 的 cookiepre）。
    统一说成"以 _auth 和 _saltkey 结尾"，用户按这个找一定能找到。
    """
    prefix = f"重新登录 {site_name} 后，" if site_name else "重新登录后"
    return (
        f"{prefix}从浏览器复制整段 Cookie（F12 → 网络 → 请求标头），"
        "要含以 _auth 和 _saltkey 结尾的两条（Discuz 的登录票据，"
        "各站前缀不同，认后缀即可）"
    )


def login_verdict(html: str, site_name: str = "") -> tuple[bool, str]:
    """统一给出 (是否登录, 说明)。

    这是给 verify() 和 run() 共用的一处判断，避免各站点各写一套逻辑走偏。
    """
    if logged_in(html):
        who = nickname(html)
        return True, (f"Cookie 有效：{who}" if who else "Cookie 有效（已登录）")
    if is_guest(html):
        return False, f"Cookie 已失效（页面是游客态）。{cookie_hint(site_name)}"
    return False, (
        f"Cookie 已失效或不全：页面里没有登录态（既没有退出链接，也没有游客提示）。"
        f"{cookie_hint(site_name)}"
    )
