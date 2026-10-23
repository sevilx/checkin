"""LinuxDo（linux.do）：登录保活 + 浏览任务 + Connect 信息。

linux.do 是 Discourse 论坛，**没有传统意义上的"签到按钮"**，社区里的签到脚本做的其实是：
用 Cookie 校验登录状态 → 随机浏览几个主题（保持活跃）→ 读取 Connect 页的信任等级信息。
本模块做的也是这三件事，不刷帖、不点赞、不发内容。

403 有四种完全不同的原因，必须分开说，否则用户会一直换 cookie：
* 站点回的是 Cloudflare 挑战页 → 出口 IP / TLS 指纹的问题（已用 curl_cffi 多指纹兜底）；
* Cookie 里没有 `_t` → Discourse 的登录令牌是 HttpOnly，控制台 document.cookie 拿不到，
  只能从「网络 → 请求标头 → Cookie」整行复制；
* Cookie 里有 `_t` 但站点不认 → 那是别的环境签发的，重新登录一次；
* 出口 IP 被 linux.do 的风控盯上 → 换节点。
"""

from __future__ import annotations

import random

from ..core import Line, Site, XHR_HEADERS, parse_cookie

BASE = "https://linux.do"


class LinuxDo(Site):
    key = "LINUXDO"
    name = "LinuxDo 论坛"
    fields = ("cookie", "browse")
    doc = (
        "登录 linux.do → F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（必须有 _t，它是 HttpOnly，控制台 document.cookie 里没有）；browse=浏览主题数，默认 3"
    )
    cf_impersonates = ("safari15_5", "chrome120", "chrome110")

    def __init__(self, account: dict, index: int = 0):
        super().__init__(account, index)
        cookies = parse_cookie(self.cookie)
        self.has_token = bool(cookies.get("_t"))
        if not self.has_token and not cookies.get("_forum_session"):
            raise ValueError(
                "Cookie 里既没有 _t 也没有 _forum_session，复制到的不是 linux.do 的 Cookie："
                "F12 → 网络 → 点任意请求 → 请求标头 → 把 Cookie 整行复制过来"
            )

    def _headers(self, kept_cf: bool) -> dict:
        return self.cf_headers(
            {
                **XHR_HEADERS,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}/",
                "Origin": BASE,
            },
            kept_cf,
        )

    def run(self) -> list[Line]:
        cookie_str, kept_cf = self.cf_cookies()
        headers = self._headers(kept_cf)

        current, session = self.browser_request(
            lambda sess: sess.get(f"{BASE}/session/current.json", timeout=25),
            cookie=cookie_str,
            headers=headers,
        )
        self._guard(current)

        try:
            user = (current.json() or {}).get("current_user") or {}
        except ValueError:
            raise RuntimeError("登录校验返回非 JSON：站点返回的不像接口数据，可能被拦截了") from None

        if not user.get("username"):
            raise RuntimeError("Cookie 已失效（接口能访问，但没有登录态），重新登录后复制一次完整 Cookie")

        lines = [
            Line("账号", user.get("username")),
            Line("信任等级", f"TL{user.get('trust_level', '?')}"),
        ]

        # 浏览任务：只读 GET，模拟正常用户翻几个帖子
        try:
            count = int(str(self.account.get("browse") or 3))
        except ValueError:
            count = 3
        count = max(0, min(count, 10))

        viewed = 0
        if count:
            latest = session.get(f"{BASE}/latest.json?order=default", timeout=25)
            topics = []
            try:
                topics = ((latest.json() or {}).get("topic_list") or {}).get("topics") or []
            except ValueError:
                pass
            for topic in random.sample(topics, min(count, len(topics))):
                tid = topic.get("id")
                if not tid:
                    continue
                try:
                    detail = session.get(f"{BASE}/t/{tid}.json", timeout=25)
                    if detail.status_code == 200:
                        viewed += 1
                except Exception:  # noqa: BLE001
                    continue
            lines.append(Line("浏览主题", f"{viewed}/{count}"))

        # Connect 汇总信息（信任等级进度）
        try:
            connect = session.get("https://connect.linux.do/", timeout=25)
            if connect.status_code == 200 and "信任级别" in connect.text:
                lines.append(Line("Connect", "信息页可访问（信任等级进度见网页）"))
        except Exception:  # noqa: BLE001
            pass

        return lines

    # ------------------------------------------------------------------ #
    def _guard(self, resp) -> None:
        """把非 200 的结果翻译成能照做的动作；风控拦截已由 browser_request 提前抛掉。"""
        if resp.status_code == 200:
            return
        if resp.status_code in (401, 403):
            if not self.has_token:
                raise RuntimeError(
                    "Cookie 里没有 _t（Discourse 的登录令牌）：它是 HttpOnly，"
                    "控制台里输 document.cookie 是拿不到的。"
                    "请用 F12 → 网络 → 任意请求 → 请求标头 → Cookie 整行复制。"
                )
            raise RuntimeError(
                f"登录校验被拒（HTTP {resp.status_code}），域名能解析、站点也回话了 —— "
                "Cookie 里的 _t 已不被承认（改密码 / 手动退出登录 / 换设备都会让它失效），"
                "重新登录一次再复制完整 Cookie"
            )
        raise RuntimeError(f"登录校验失败 HTTP {resp.status_code}")

    def verify(self) -> tuple[bool, str]:
        """只读检测：Discourse 的 /session/current.json，返回 current_user 即有效。"""
        cookie_str, kept_cf = self.cf_cookies()
        resp, _ = self.browser_request(
            lambda sess: sess.get(f"{BASE}/session/current.json", timeout=20),
            cookie=cookie_str,
            headers=self._headers(kept_cf),
        )
        if resp.status_code == 200:
            try:
                user = resp.json().get("current_user") or {}
            except ValueError:
                return False, "返回非 JSON，可能被拦截"
            if user.get("username"):
                return True, f"Cookie 有效：{user['username']}（信任等级 {user.get('trust_level', '?')}）"
            return False, "接口能访问但没有登录态，Cookie 已失效"
        if resp.status_code in (403, 404):
            return False, "Cookie 已失效（未登录）"
        return False, f"检测失败：HTTP {resp.status_code}"
