"""NodeSeek：POST /api/attendance 领取鸡腿。

注意两个坑：
1. 今天已签到时接口返回 HTTP 500，但 body 仍是 JSON，所以只能看 success 字段；
2. cf_clearance 不是必需的，只带 session 就能签。
"""

from __future__ import annotations

import base64
import json

from ..core import Line, Site, XHR_HEADERS, looks_blocked, parse_cookie

BASE = "https://www.nodeseek.com"
ATTENDANCE_URL = f"{BASE}/api/attendance"
DONE_HINTS = ("已完成签到", "已经签到", "已签到", "请勿重复")


class NodeSeek(Site):
    key = "NODESEEK"
    name = "NodeSeek"
    fields = ("cookie", "method")
    doc = "登录 nodeseek.com → F12 → Cookie（至少含 session）；method 可选 random / fixed"

    def __init__(self, account: dict, index: int = 0):
        super().__init__(account, index)
        cookies = parse_cookie(self.cookie)
        if "session" not in cookies:
            raise ValueError("Cookie 中未找到 session，请重新登录后复制")
        self.username = self._decode_name(cookies.get("pjwt", ""))
        self.has_clearance = bool(cookies.get("cf_clearance"))
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
        session = self.session(
            headers={
                **XHR_HEADERS,
                "Referer": f"{BASE}/",
                "Origin": BASE,
            }
        )
        resp = session.post(
            f"{ATTENDANCE_URL}?random={'true' if self.method == 'random' else 'false'}",
            timeout=25,
        )

        if looks_blocked(resp.status_code, resp.text):
            raise RuntimeError(self._blocked_hint(resp.status_code))

        try:
            data = resp.json()
        except ValueError:
            body = (resp.text or "").strip().replace("\n", " ")[:100]
            raise RuntimeError(
                f"返回非 JSON（HTTP {resp.status_code}）: {body} —— "
                "通常是被 Cloudflare 拦了，见下方说明"
            ) from None

        lines: list[Line] = []
        if self.username:
            lines.append(Line("账号", self.username))
        lines.append(Line("签到方式", "随机鸡腿" if self.method == "random" else "固定 5 鸡腿"))
        if not self.has_clearance:
            lines.append(Line("提示", "Cookie 里没有 cf_clearance，代理换 IP 后可能突然 403"))

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

    def _blocked_hint(self, status: int) -> str:
        return (
            f"被 Cloudflare 拦截（HTTP {status}），请求没到 NodeSeek 服务器。三个办法：\n"
            "1) 浏览器打开 nodeseek.com 完成一次验证，然后把 cf_clearance 一并复制进 Cookie；\n"
            "2) 代理要固定出口 IP —— 每次换 IP 都会让 cf_clearance 失效；\n"
            "3) 用直连或家宽出口，机房 IP 更容易被拦。"
        )

    def verify(self) -> tuple[bool, str]:
        """只读检测：GET /api/account，能拿到用户名说明 session 有效。"""
        session = self.session(headers={**XHR_HEADERS, "Referer": f"{BASE}/"})
        resp = session.get(f"{BASE}/api/account", timeout=20)
        if looks_blocked(resp.status_code, resp.text):
            return False, f"被 Cloudflare 拦截（HTTP {resp.status_code}），出口 IP 或 cf_clearance 的问题"
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return False, "返回非 JSON，可能被拦截"
            name = ((data or {}).get("member") or {}).get("member_name") or (data or {}).get("name")
            return True, f"Cookie 有效：{name}" if name else "Cookie 有效"
        return False, f"检测失败：HTTP {resp.status_code}"
