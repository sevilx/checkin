"""LinuxDo（linux.do）：登录保活 + 浏览任务 + Connect 信息。

linux.do 是 Discourse 论坛，**没有传统意义上的"签到按钮"**，社区里的签到脚本做的其实是：
用 Cookie 校验登录状态 → 随机浏览几个主题（保持活跃）→ 读取 Connect 页的信任等级信息。
本模块做的也是这三件事，不刷帖、不点赞、不发内容。

403 有两种完全不同的原因，必须分开说，否则用户会一直换 cookie：
* 站点回的是 Cloudflare 拦截页 → 出口 IP 的问题，换代理；
* 站点能打开但接口拒绝 → cookie 不完整（缺 _t / _forum_session）。
"""

from __future__ import annotations

import random

from ..core import Line, NAV_HEADERS, Site, XHR_HEADERS, looks_blocked

BASE = "https://linux.do"


class LinuxDo(Site):
    key = "LINUXDO"
    name = "LinuxDo 论坛"
    fields = ("cookie", "browse")
    doc = "登录 linux.do → F12 → Cookie 整段复制（含 _t、_forum_session）；browse=要浏览的主题数，默认 3"

    def run(self) -> list[Line]:
        session = self.session(
            headers={
                **XHR_HEADERS,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}/",
            }
        )

        current = session.get(f"{BASE}/session/current.json", timeout=25)
        self._guard(current)

        try:
            user = (current.json() or {}).get("current_user") or {}
        except ValueError:
            raise RuntimeError("登录校验返回非 JSON，可能被 Cloudflare 拦截") from None

        if not user.get("username"):
            raise RuntimeError("Cookie 已失效（未取到当前用户，重新登录后复制一次完整 Cookie）")

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
        """把 403 拆成"被风控"和"cookie 不完整"两种，别让用户白折腾。"""
        if resp.status_code == 200:
            return
        if looks_blocked(resp.status_code, resp.text):
            raise RuntimeError(
                f"被 Cloudflare 拦截（HTTP {resp.status_code}）：请求没到 linux.do。"
                "换一个代理出口（机房 IP 容易被拦），或在浏览器里过一次验证后把 cf_clearance 一起复制进 Cookie"
            )
        if resp.status_code in (401, 403):
            try:
                root = self.session(headers={**NAV_HEADERS, "Referer": f"{BASE}/"}).get(
                    f"{BASE}/", timeout=20
                )
                reachable = root.status_code == 200
            except Exception:  # noqa: BLE001
                reachable = False
            if reachable:
                raise RuntimeError(
                    f"登录校验被拒（HTTP {resp.status_code}），但站点本身能打开 —— "
                    "多半是 Cookie 不完整（缺 _t / _forum_session）或这个出口 IP 被风控。"
                    "重新登录复制一次完整 Cookie，或换个代理"
                )
            raise RuntimeError(f"无法访问 linux.do（HTTP {resp.status_code}），检查代理是否可用")
        raise RuntimeError(f"登录校验失败 HTTP {resp.status_code}")

    def verify(self) -> tuple[bool, str]:
        """只读检测：Discourse 的 /session/current.json，返回 current_user 即有效。"""
        session = self.session(headers={**XHR_HEADERS, "Referer": f"{BASE}/"})
        resp = session.get(f"{BASE}/session/current.json", timeout=20)
        if looks_blocked(resp.status_code, resp.text):
            return False, f"被 Cloudflare 拦截（HTTP {resp.status_code}），换个出口 IP 或补 cf_clearance"
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
