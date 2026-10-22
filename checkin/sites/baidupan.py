"""百度网盘：会员成长值签到 + 每日答题 + 会员信息。"""

from __future__ import annotations

import re
import time

from ..core import Line, Site

BASE = "https://pan.baidu.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/90.0.4430.91 Mobile Safari/537.36"
    ),
    "Referer": "https://pan.baidu.com/wap/svip/growth/task",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _search(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


class BaiduPan(Site):
    key = "BAIDUWP"
    name = "百度网盘"
    fields = ("cookie",)
    doc = "登录 pan.baidu.com → F12 → Cookie 整段复制（必须含 BDUSS、STOKEN）"

    def _get(self, session, path: str, params: dict):
        resp = session.get(f"{BASE}{path}", params=params, timeout=20)
        if resp.status_code != 200:
            raise RuntimeError(f"请求失败 HTTP {resp.status_code}")
        return resp.text

    def run(self) -> list[Line]:
        session = self.session(headers=HEADERS)

        sign_text = self._get(
            session, "/rest/2.0/membership/level", {"app_id": "250528", "web": "5", "method": "signin"}
        )
        points = _search(r'"points":(\d+)', sign_text)
        sign_msg = _search(r'"error_msg":"(.*?)"', sign_text)
        if points is None and not sign_msg:
            raise RuntimeError(f"签到返回无法解析，Cookie 可能已失效: {sign_text[:120]}")
        lines = [Line("签到", f"+{points} 成长值" if points else (sign_msg or "今日已签到"))]

        time.sleep(2)
        ask_id = answer = None
        try:
            question_text = self._get(
                session, "/act/v2/membergrowv2/getdailyquestion", {"app_id": "250528", "web": "5"}
            )
            ask_id = _search(r'"ask_id":(\d+)', question_text)
            answer = _search(r'"answer":(\d+)', question_text)
        except Exception as exc:  # noqa: BLE001
            lines.append(Line("答题", f"获取题目失败: {exc}"))

        if ask_id and answer:
            try:
                answer_text = self._get(
                    session,
                    "/act/v2/membergrowv2/answerquestion",
                    {"app_id": "250528", "web": "5", "ask_id": ask_id, "answer": answer},
                )
                score = _search(r'"score":(\d+)', answer_text)
                show_msg = _search(r'"show_msg":"(.*?)"', answer_text)
                lines.append(Line("答题", f"+{score} 成长值" if score else (show_msg or "已完成")))
            except Exception as exc:  # noqa: BLE001
                lines.append(Line("答题", f"提交失败: {exc}"))

        user_text = self._get(
            session, "/rest/2.0/membership/user", {"app_id": "250528", "web": "5", "method": "query"}
        )
        level = _search(r'"current_level":(\d+)', user_text)
        value = _search(r'"current_value":(\d+)', user_text)
        if level or value:
            lines.append(Line("会员", f"V{level or '?'}，成长值 {value or '?'}"))
        return lines


    def verify(self) -> tuple[bool, str]:
        """只读检测：/api/quota，errno=0 表示 cookie 有效。"""
        session = self.session(headers=HEADERS)
        resp = session.get(
            f"{BASE}/api/quota", params={"checkfree": "1", "checkexpire": "1"}, timeout=20
        )
        data = resp.json()
        errno = data.get("errno")
        if errno == 0:
            total = data.get("total")
            size = f"，总容量 {total / 1024 ** 3:.1f} G" if isinstance(total, (int, float)) and total else ""
            return True, f"Cookie 有效{size}"
        if errno == -6:
            return False, "Cookie 已失效（BDUSS 无效或已过期）"
        detail = f"{data.get('error_msg') or ''}".strip()
        return False, f"检测失败：errno={errno}{' ' + detail if detail else ''}"
