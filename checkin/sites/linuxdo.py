"""LinuxDo（linux.do）：登录保活 + 浏览任务 + Connect 信息。

linux.do 是 Discourse 论坛，**没有传统意义上的"签到按钮"**，社区里的签到脚本做的其实是：
用 Cookie 校验登录状态 → 随机浏览几个主题（保持活跃）→ 读取 Connect 页的信任等级信息。
本模块做的也是这三件事，不刷帖、不点赞、不发内容。
"""

from __future__ import annotations

import random

from ..core import Line, Site

BASE = "https://linux.do"


class LinuxDo(Site):
    key = "LINUXDO"
    name = "LinuxDo 论坛"
    fields = ("cookie", "browse")
    doc = "登录 linux.do → F12 → Cookie 整段复制（含 _t、_forum_session）；browse=要浏览的主题数，默认 3"

    def run(self) -> list[Line]:
        session = self.session(
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}/",
            }
        )

        current = session.get(f"{BASE}/session/current.json", timeout=25)
        if current.status_code != 200:
            raise RuntimeError(f"登录校验失败 HTTP {current.status_code}，Cookie 可能已失效")
        try:
            user = (current.json() or {}).get("current_user") or {}
        except ValueError:
            raise RuntimeError("登录校验返回非 JSON，可能被 Cloudflare 拦截") from None

        if not user.get("username"):
            raise RuntimeError("Cookie 已失效（未取到当前用户）")

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
            topics = ((latest.json() or {}).get("topic_list") or {}).get("topics") or []
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


    def verify(self) -> tuple[bool, str]:
        """只读检测：Discourse 的 /session/current.json，返回 current_user 即有效。"""
        session = self.session(headers={"Accept": "application/json", "Referer": f"{BASE}/"})
        resp = session.get(f"{BASE}/session/current.json", timeout=20)
        if resp.status_code == 200:
            user = resp.json().get("current_user") or {}
            if user.get("username"):
                return True, f"Cookie 有效：{user['username']}（信任等级 {user.get('trust_level', '?')}）"
        if resp.status_code in (403, 404):
            return False, "Cookie 已失效（未登录）"
        return False, f"检测失败：HTTP {resp.status_code}"
