"""什么值得买：APP 接口签到（robot token + checkin + 奖励明细）。"""

from __future__ import annotations

import hashlib
import time

from ..core import Line, Site

SIGN_KEY = "apr1$AwP!wRRT$gJ/q.X24poeBInlUJC"
APP_SK = "ierkM0OZZbsuBKLoAgQ6OJneLMXBQXmzX+LXkNTuKch8Ui2jGlahuFyWIzBiDq/L"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


class Smzdm(Site):
    key = "SMZDM"
    name = "什么值得买"
    fields = ("cookie",)
    doc = "登录 smzdm.com → F12 → Cookie 整段复制（建议整块复制，sess 不能少）"

    def run(self) -> list[Line]:
        cookie = self.cookie
        headers = {
            "Host": "user-api.smzdm.com",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie,
            "User-Agent": "smzdm_android_V10.4.1 rv:841 (22021211RC;Android12;zh)smzdmapp",
        }
        session = self.session(headers=headers, cookie=cookie)

        ts = round(time.time() * 1000)
        token_resp = session.post(
            "https://user-api.smzdm.com/robot/token",
            data={
                "f": "android",
                "v": "10.4.1",
                "weixin": 1,
                "time": ts,
                "sign": _md5(f"f=android&time={ts}&v=10.4.1&weixin=1&key={SIGN_KEY}"),
            },
            timeout=20,
        ).json()
        token = (token_resp.get("data") or {}).get("token")
        if not token:
            raise RuntimeError(f"获取 token 失败: {token_resp.get('error_msg') or token_resp}")

        ts2 = round(time.time() * 1000)
        payload = {
            "f": "android",
            "v": "10.4.1",
            "sk": APP_SK,
            "weixin": 1,
            "time": ts2,
            "token": token,
            "sign": _md5(f"f=android&sk={APP_SK}&time={ts2}&token={token}&v=10.4.1&weixin=1&key={SIGN_KEY}"),
        }
        sign_resp = session.post("https://user-api.smzdm.com/checkin", data=payload, timeout=20).json()
        msg = sign_resp.get("error_msg") or "未知结果"
        lines = [Line("签到", msg)]

        try:
            reward = session.post(
                "https://user-api.smzdm.com/checkin/all_reward", data=payload, timeout=20
            ).json()
            normal = (reward.get("data") or {}).get("normal_reward") or {}
            if normal:
                content = (normal.get("reward_add") or {}).get("content")
                if content:
                    lines.append(Line("奖励", content))
                if normal.get("sub_title"):
                    lines.append(Line("连续签到", normal["sub_title"]))
        except Exception as exc:  # noqa: BLE001
            lines.append(Line("奖励", f"读取失败: {exc}"))
        return lines
