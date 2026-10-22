"""飞牛 NAS 论坛（club.fnnas.com）：Discuz 打卡插件。"""

from __future__ import annotations

import re

from ..core import Line, Site

BASE = "https://club.fnnas.com"
SIGN_PAGE = f"{BASE}/plugin.php?id=zqlj_sign"

SIGN_BTN = re.compile(r'<a href="plugin\.php\?id=zqlj_sign&sign=([0-9a-fA-F]+)" class="btna">(点击打卡|今日已打卡)</a>')


class FnNasClub(Site):
    key = "FNNASCLUB"
    name = "飞牛 NAS 论坛"
    fields = ("cookie",)
    doc = "登录 club.fnnas.com → F12 → Cookie 整段复制（必须含 pvRK_2132_auth）"

    def run(self) -> list[Line]:
        session = self.session(
            headers={
                "Referer": f"{BASE}/portal.php",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

        page = session.get(SIGN_PAGE, timeout=20)
        if page.status_code != 200:
            raise RuntimeError(f"打开签到页失败 HTTP {page.status_code}")

        match = SIGN_BTN.search(page.text)
        if not match:
            if "登录" in page.text and "退出" not in page.text:
                raise RuntimeError("Cookie 已失效（签到页需要登录）")
            raise RuntimeError("未找到签到按钮，插件可能改版或被风控")

        sign_param, button_text = match.group(1), match.group(2)
        lines: list[Line] = []

        if button_text == "今日已打卡":
            lines.append(Line("签到", "今日已打卡"))
        else:
            resp = session.get(f"{SIGN_PAGE}&sign={sign_param}", timeout=20)
            if "恭喜您，打卡成功" in resp.text:
                lines.append(Line("签到", "打卡成功"))
            elif "您今天已经打过卡了" in resp.text:
                lines.append(Line("签到", "今日已打卡"))
            else:
                raise RuntimeError(f"签到结果未知，HTTP {resp.status_code}")

        # 打卡动态（连续天数 / 奖励）
        try:
            detail = session.get(SIGN_PAGE, timeout=20).text
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
