"""飞牛 NAS 论坛（club.fnnas.com）：Discuz 打卡插件。

这个站不太稳定，偶尔整体 503（维护 / 限流），所以对 5xx 做三次退避重试；
重试仍失败时明确说是"站点不可用"，而不是让用户去怀疑 cookie。
"""

from __future__ import annotations

import re
import time

from ..core import Line, NAV_HEADERS, Site, looks_blocked

BASE = "https://club.fnnas.com"
SIGN_PAGE = f"{BASE}/plugin.php?id=zqlj_sign"

SIGN_BTN = re.compile(
    r'<a href="plugin\.php\?id=zqlj_sign&(?:amp;)?sign=([0-9a-fA-F]+)"[^>]*>(点击打卡|今日已打卡)</a>'
)


class FnNasClub(Site):
    key = "FNNASCLUB"
    name = "飞牛 NAS 论坛"
    fields = ("cookie",)
    doc = "登录 club.fnnas.com → F12 → Cookie 整段复制（必须含 pvRK_2132_auth）"

    @staticmethod
    def _get(session, url: str, **kwargs):
        """5xx 重试 3 次：这个站的 503 多数是瞬时的。"""
        resp = None
        for attempt in range(3):
            resp = session.get(url, timeout=20, **kwargs)
            if resp.status_code < 500:
                return resp
            if attempt < 2:
                time.sleep(1.5 + attempt * 2.5)
        return resp

    def run(self) -> list[Line]:
        session = self.session(
            headers={
                **NAV_HEADERS,
                "Referer": f"{BASE}/portal.php",
            }
        )

        page = self._get(session, SIGN_PAGE)
        if page.status_code >= 500:
            text = page.text or ""
            if "升级维护" in text or "维护中" in text:
                raise RuntimeError(
                    "飞牛论坛正在升级维护（站点自己返回的页面写着“升级维护中”），"
                    "和 cookie 无关，过几小时再试"
                )
            raise RuntimeError(
                f"飞牛论坛返回 HTTP {page.status_code}（已重试 3 次）—— 站点维护或被限流，过几小时再试"
            )
        if page.status_code != 200:
            if looks_blocked(page.status_code, page.text):
                raise RuntimeError(f"被防火墙拦截（HTTP {page.status_code}），换个网络或代理再试")
            raise RuntimeError(f"打开签到页失败 HTTP {page.status_code}")

        match = SIGN_BTN.search(page.text)
        if not match:
            if "登录" in page.text and "退出" not in page.text:
                raise RuntimeError("Cookie 已失效（签到页要求登录）")
            raise RuntimeError("未找到打卡按钮，插件可能改版或被风控")

        sign_param, button_text = match.group(1), match.group(2)
        lines: list[Line] = []

        if button_text == "今日已打卡":
            lines.append(Line("签到", "今日已打卡"))
        else:
            resp = self._get(session, f"{SIGN_PAGE}&sign={sign_param}", headers={"Referer": SIGN_PAGE})
            text = resp.text or ""
            if "恭喜您，打卡成功" in text:
                lines.append(Line("签到", "打卡成功"))
            elif "您今天已经打过卡了" in text:
                lines.append(Line("签到", "今日已打卡"))
            elif resp.status_code >= 500:
                raise RuntimeError(f"打卡请求返回 HTTP {resp.status_code}（已重试 3 次），站点可能正在维护")
            else:
                raise RuntimeError(f"打卡结果未知，HTTP {resp.status_code}")

        # 打卡动态（连续天数 / 奖励）
        try:
            detail = self._get(session, SIGN_PAGE, headers={"Referer": SIGN_PAGE}).text
            block = re.search(
                r"<strong>\s*我的打卡动态\s*</strong>.*?(<div[^>]*class=\"bm_c\"[^>]*>.*?</div>)",
                detail,
                re.S,
            )
            if block:
                text = re.sub(r"<[^>]+>", "", re.sub(r"</li\s*>", "</li>\n", block.group(1)))
                for line in (item.strip() for item in text.splitlines()):
                    if not line:
                        continue
                    sep = "：" if "：" in line else (":" if ":" in line else None)
                    if not sep:
                        continue
                    key, _, value = line.partition(sep)
                    lines.append(Line(key.strip(), value.strip()))
        except Exception as exc:  # noqa: BLE001
            lines.append(Line("打卡动态", f"读取失败: {exc}"))

        return lines
