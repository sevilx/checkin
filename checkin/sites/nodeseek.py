"""NodeSeek：POST /api/attendance 领取鸡腿。

三个坑：
1. 今天已签到时接口返回 HTTP 500，但 body 仍是 JSON，所以只能看 success 字段；
2. NodeSeek 挂在 Cloudflare 后面，requests 的 TLS 握手会被直接挑战 ——
   所以要模仿真实浏览器的指纹（curl_cffi），并换几个指纹重试；
3. 浏览器里带出来的 cf_clearance 和 IP + UA 绑定，换机器就失效，默认丢掉。
"""

from __future__ import annotations

import base64
import json

from ..core import Line, Site, XHR_HEADERS, parse_cookie

BASE = "https://www.nodeseek.com"
ATTENDANCE_URL = f"{BASE}/api/attendance"
DONE_HINTS = ("已完成签到", "已经签到", "已签到", "请勿重复")


class NodeSeek(Site):
    key = "NODESEEK"
    name = "NodeSeek"
    fields = ("cookie", "method")
    doc = "登录 nodeseek.com → F12 → Cookie（至少含 session，不要只抄 cf_clearance）；method 可选 random / fixed"
    cf_impersonates = ("safari15_5", "chrome120", "chrome110")

    def __init__(self, account: dict, index: int = 0):
        super().__init__(account, index)
        cookies = parse_cookie(self.cookie)
        if "session" not in cookies:
            raise ValueError("Cookie 中未找到 session，请重新登录后复制")
        self.username = self._decode_name(cookies.get("pjwt", ""))
        raw = str(self.account.get("method") or "random").strip().lower()
        self.method = "fixed" if raw in ("fixed", "fixed5", "5", "constant") else "random"

    @staticmethod
    def _decode_name(pjwt: str) -> str:
        parts = str(pjwt or "").strip().split(".")
        if len(parts) < 2:
            return ""
        payload = parts[-2] + "=" * (-len(parts[-2]) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        except Exception:  # noqa: BLE001
            return ""
        return str((data or {}).get("name") or "")

    def run(self) -> list[Line]:
        cookie_str, kept_cf = self.cf_cookies()
        headers = self.cf_headers(
            {**XHR_HEADERS, "Referer": f"{BASE}/", "Origin": BASE}, kept_cf
        )
        url = f"{ATTENDANCE_URL}?random={'true' if self.method == 'random' else 'false'}"
        # 带一个空 JSON body：和站点前端发的请求一致，空 body 的 POST 更容易被 WAF 挑刺
        resp, _ = self.browser_request(
            lambda session: session.post(url, json={}, timeout=25),
            cookie=cookie_str,
            headers=headers,
        )

        try:
            data = resp.json()
        except ValueError:
            body = (resp.text or "").strip().replace("\n", " ")[:100]
            raise RuntimeError(
                f"返回非 JSON（HTTP {resp.status_code}）: {body} —— "
                "站点没返回数据，通常是登录态失效或页面结构变了"
            ) from None

        lines: list[Line] = []
        if self.username:
            lines.append(Line("账号", self.username))
        lines.append(Line("签到方式", "随机鸡腿" if self.method == "random" else "固定 5 鸡腿"))

        message = str(data.get("message") or data.get("msg") or "")
        if data.get("success"):
            gain, current = data.get("gain"), data.get("current")
            text = f"+{gain} 鸡腿" if gain is not None else message
            if current is not None:
                text += f"，当前 {current} 鸡腿"
            lines.append(Line("签到", text))
            return lines

        if any(hint in message for hint in DONE_HINTS):
            current = data.get("current")
            lines.append(Line("签到", f"今日已签到{'' if current is None else f'，当前 {current} 鸡腿'}"))
            return lines

        raise RuntimeError(f"签到失败: {message or f'HTTP {resp.status_code}'}")

    def verify(self) -> tuple[bool, str]:
        """只读检测：GET /api/account，能拿到用户名说明 session 有效。"""
        cookie_str, kept_cf = self.cf_cookies()
        headers = self.cf_headers({**XHR_HEADERS, "Referer": f"{BASE}/"}, kept_cf)
        resp, _ = self.browser_request(
            lambda session: session.get(f"{BASE}/api/account", timeout=20),
            cookie=cookie_str,
            headers=headers,
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return False, "返回非 JSON，可能被拦截"
            name = ((data or {}).get("member") or {}).get("member_name") or (data or {}).get("name")
            return True, f"Cookie 有效：{name}" if name else "Cookie 有效"
        if resp.status_code in (401, 403):
            return False, "Cookie 已失效（session 不被认可），重新登录再复制一次"
        return False, f"检测失败：HTTP {resp.status_code}"

