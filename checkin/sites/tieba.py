"""百度贴吧：按关注列表逐吧签到（客户端接口 + MD5 签名）。"""

from __future__ import annotations

import hashlib
import random
import time

from ..core import Line, Site

TBS_URL = "http://tieba.baidu.com/dc/common/tbs"
LIKE_URL = "http://c.tieba.baidu.com/c/f/forum/like"
SIGN_URL = "http://c.tieba.baidu.com/c/c/forum/sign"
LOGIN_INFO_URL = "https://zhidao.baidu.com/api/loginInfo"
SIGN_KEY = "tiebaclient!!!"

SIGN_DATA = {
    "_client_type": "2",
    "_client_version": "9.7.8.0",
    "_phone_imei": "000000000000000",
    "model": "MI+5",
    "net_type": "1",
}


class Tieba(Site):
    key = "TIEBA"
    name = "百度贴吧"
    fields = ("cookie",)
    doc = "登录 tieba.baidu.com → F12 → Cookie 整段复制（必须含 BDUSS）"

    def __init__(self, account: dict, index: int = 0):
        super().__init__(account, index)
        from ..core import parse_cookie

        self.bduss = parse_cookie(self.cookie).get("BDUSS", "")
        if not self.bduss:
            raise ValueError("Cookie 中未找到 BDUSS")

    # ------------------------------------------------------------------ #
    def _encode(self, data: dict) -> dict:
        payload = dict(data)
        joined = "".join(f"{key}={payload[key]}" for key in sorted(payload))
        payload["sign"] = hashlib.md5((joined + SIGN_KEY).encode("utf-8")).hexdigest().upper()
        return payload

    def _request(self, session, url: str, method: str = "get", data: dict | None = None, retry: int = 3) -> dict:
        last = None
        for attempt in range(retry):
            try:
                if method == "get":
                    resp = session.get(url, timeout=15)
                else:
                    resp = session.post(url, data=data, timeout=15)
                resp.raise_for_status()
                if not resp.text.strip():
                    raise ValueError("空响应")
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < retry - 1:
                    time.sleep(1.5 * (2**attempt) + random.uniform(0, 1))
        raise RuntimeError(f"请求失败: {last}")

    def _get_forums(self, session) -> list[dict]:
        forums: list[dict] = []
        page = 1
        while True:
            data = {
                "BDUSS": self.bduss,
                "_client_type": "2",
                "_client_id": "wappc_1534235498291_488",
                "_client_version": "9.7.8.0",
                "_phone_imei": "000000000000000",
                "from": "1008621y",
                "page_no": str(page),
                "page_size": "200",
                "model": "MI+5",
                "net_type": "1",
                "timestamp": str(int(time.time())),
                "vcode_tag": "11",
            }
            res = self._request(session, LIKE_URL, "post", self._encode(data))
            listing = res.get("forum_list") or {}
            for group in ("non-gconforum", "gconforum"):
                items = listing.get(group)
                if isinstance(items, list):
                    forums.extend(items)
                elif isinstance(items, dict):
                    forums.append(items)
            if str(res.get("has_more")) != "1":
                break
            page += 1
            time.sleep(random.uniform(1, 2))
        return [f for f in forums if f.get("name")]

    def run(self) -> list[Line]:
        session = self.session(
            headers={
                "Host": "tieba.baidu.com",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",
            }
        )

        tbs_resp = self._request(session, TBS_URL)
        if not tbs_resp.get("is_login"):
            raise RuntimeError("登录失败，Cookie 可能已过期")
        tbs = tbs_resp.get("tbs", "")

        lines: list[Line] = []
        try:
            info = self._request(session, LOGIN_INFO_URL)
            if info.get("userName"):
                lines.append(Line("账号", info["userName"]))
        except Exception:  # noqa: BLE001
            pass

        forums = self._get_forums(session)
        if not forums:
            raise RuntimeError("未取到关注的贴吧列表")

        success = exist = shield = failed = 0
        for idx, forum in enumerate(forums):
            if idx:
                time.sleep(1.0 + random.uniform(0.3, 1.2))
            if idx and idx % 10 == 0:
                time.sleep(random.uniform(4, 8))
            data = dict(SIGN_DATA)
            data.update(
                {
                    "BDUSS": self.bduss,
                    "fid": forum.get("id", ""),
                    "kw": forum.get("name", ""),
                    "tbs": tbs,
                    "timestamp": str(int(time.time())),
                }
            )
            try:
                res = self._request(session, SIGN_URL, "post", self._encode(data))
                code = str(res.get("error_code", ""))
                if code == "0":
                    success += 1
                elif code == "160002":
                    exist += 1
                elif code == "340006":
                    shield += 1
                else:
                    failed += 1
            except Exception:  # noqa: BLE001
                failed += 1

        lines += [
            Line("贴吧总数", len(forums)),
            Line("签到成功", success),
            Line("今日已签", exist),
            Line("被屏蔽", shield),
            Line("签到失败", failed),
        ]
        return lines


    def verify(self) -> tuple[bool, str]:
        """只读检测：dc/common/tbs 的 is_login 字段。"""
        session = self.session(headers={"Referer": "https://tieba.baidu.com/"})
        data = session.get(TBS_URL, timeout=20).json()
        if str(data.get("is_login")) == "1":
            return True, "BDUSS 有效，已登录"
        return False, "BDUSS 已失效（接口返回未登录）"
