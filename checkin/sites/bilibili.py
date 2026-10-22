"""哔哩哔哩：直播签到 + 漫画签到 + 大会员权益领取。

不投币、不换瓜子 —— 那些动作会消耗资产，交给人工决定。
"""

from __future__ import annotations

from ..core import Line, Site


class Bilibili(Site):
    key = "BILIBILI"
    name = "哔哩哔哩"
    fields = ("cookie",)
    doc = "登录 bilibili.com → F12 → Cookie 整段复制（必须含 SESSDATA、bili_jct）"

    def run(self) -> list[Line]:
        session = self.session(
            headers={
                "Referer": "https://www.bilibili.com/",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        csrf = str(self.account.get("csrf") or "").strip()
        if not csrf:
            from ..core import parse_cookie

            csrf = parse_cookie(self.cookie).get("bili_jct", "")

        nav = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=20).json()
        data = nav.get("data") or {}
        if not data.get("isLogin"):
            raise RuntimeError("Cookie 已失效（nav 返回未登录）")

        lines = [
            Line("账号", data.get("uname") or data.get("mid")),
            Line("等级", f"Lv{((data.get('level_info') or {}).get('current_level'))}"),
            Line("硬币", data.get("money")),
        ]

        # 直播签到
        try:
            live = session.get(
                "https://api.live.bilibili.com/xlive/web-ucenter/v1/sign/DoSign", timeout=20
            ).json()
            code = live.get("code")
            if code == 0:
                info = live.get("data") or {}
                lines.append(
                    Line(
                        "直播签到",
                        f"成功，{info.get('text', '')}（本月 {info.get('hadSignDays', '?')} 天）",
                    )
                )
            elif code == 1011040:
                lines.append(Line("直播签到", "今日已签到"))
            else:
                lines.append(Line("直播签到", f"失败: {live.get('message')}"))
        except Exception as exc:  # noqa: BLE001
            lines.append(Line("直播签到", f"异常: {exc}"))

        # 漫画签到
        try:
            manga = session.post(
                "https://manga.bilibili.com/twirp/activity.v1.Activity/ClockIn",
                data={"platform": "android"},
                timeout=20,
            ).json()
            if manga.get("code") == 0:
                lines.append(Line("漫画签到", "成功"))
            elif "duplicate" in str(manga.get("msg", "")).lower():
                lines.append(Line("漫画签到", "今日已签到"))
            else:
                lines.append(Line("漫画签到", f"失败: {manga.get('msg')}"))
        except Exception as exc:  # noqa: BLE001
            lines.append(Line("漫画签到", f"异常: {exc}"))

        # 大会员权益（B币券 / 优惠券）
        if csrf:
            try:
                my = session.get("https://api.bilibili.com/x/vip/privilege/my", timeout=20).json()
                welfare = (my.get("data") or {}).get("list") or []
                vip_type = data.get("vipType") or 0
                got = 0
                for item in welfare:
                    if item.get("state") == 0 and item.get("vip_type") == vip_type and vip_type:
                        ret = session.post(
                            "https://api.bilibili.com/x/vip/privilege/receive",
                            data={"type": item.get("type"), "csrf": csrf},
                            timeout=20,
                        ).json()
                        if ret.get("code") == 0:
                            got += 1
                if vip_type:
                    lines.append(Line("大会员权益", f"本次领取 {got} 项"))
                else:
                    lines.append(Line("大会员权益", "非大会员，跳过"))
            except Exception as exc:  # noqa: BLE001
                lines.append(Line("大会员权益", f"异常: {exc}"))

        return lines


    def verify(self) -> tuple[bool, str]:
        """只读检测：/x/web-interface/nav，code=0 表示登录态有效。"""
        session = self.session(headers={"Referer": "https://www.bilibili.com/"})
        data = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=20).json()
        code = data.get("code")
        if code == 0:
            info = data.get("data") or {}
            name = info.get("uname") or "已登录"
            tier = "大会员" if info.get("vipStatus") == 1 else "普通用户"
            return True, f"Cookie 有效：{name}（{tier}）"
        if code == -101:
            return False, "Cookie 已失效（接口返回未登录）"
        return False, f"检测失败：{data.get('message') or code}"
