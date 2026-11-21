#!/usr/bin/env python3
"""吾爱破解瑞数（WZWS）真浏览器 PoC —— 时间盒验证脚本。

只回答一件事：容器里的 headless Chromium + stealth 能不能自动过瑞数挑战、看到论坛页面。

安全约束（务必保持）：
  * 默认只读：打开页面、等挑战过去、报告检测结果，**不点签到提交**。
  * 只有显式 ``--apply`` 才做一次签到（apply + draw 各一次），不重试、不循环。
  * 内置最小间隔与每日次数上限，防的是反复撞主号触发风控。
  * Cookie 只从 ``POJIE_COOKIE`` 环境变量读，代理只从 ``POJIE_PROXY`` 读，
    脚本任何情况下都不打印它们的值。
  * 报告里的 URL 会去掉 query/fragment，避免把 wzws_cid 之类的 token 打进日志。

依赖只在 main() 里延迟导入，保证本机没装 Playwright 时 ``python probe.py --help`` 仍可用。
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BASE = "https://www.52pojie.cn"
HOME_URL = f"{BASE}/"
# 现有 checkin/sites/pojie.py 已经确认：吾爱没有 erling_qd 插件，签到走 Discuz 任务系统。
APPLY_URL = f"{BASE}/home.php?mod=task&do=apply&id=2&referer=//"
DRAW_URL = f"{BASE}/home.php?mod=task&do=draw&id=2&referer=//"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
COOKIE_DOMAIN = ".52pojie.cn"
MIN_INTERVAL_SECONDS = 60
MAX_RUNS_PER_DAY = 6

# 挑战页特征：瑞数 WZWS 的 JS 挑战页里有 wzws 字样，且没有正常 <title>。
CHALLENGE_MARKERS = (
    "wzws",
    "waf_text_verify",
    "浏览器验证",
    "正在验证",
    "请开启 javascript",
)
# 论坛特征：标题命中优先级最高；正文命中用站点名 / Discuz 版权串。
FORUM_TITLE_MARKERS = ("吾爱破解", "discuz", "52pojie")
FORUM_BODY_MARKERS = ("吾爱破解", "powered by discuz", "powered by discuz!")
# 登录态：Discuz 用 .vwmy 显示昵称，退出链接带 action=logout。
LOGIN_HINTS = ("请先登录", "尚未登录", "还没有登录", "游客", "立即注册", "登录后才能")
OK_HINTS = ("恭喜", "签到成功", "任务完成", "奖励已发放", "获得")
DONE_HINTS = (
    "已经签到", "已签到", "不是进行中的任务", "已完成", "已经完成", "已领取", "已经领取",
)
NO_TASK_HINTS = ("任务不存在", "指定的任务", "任务已关闭", "没有找到")
MESSAGE_RE = re.compile(r'id="messagetext"[^>]*>(.*?)</div>', re.S | re.I)


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def log(message: str) -> None:
    """带时间戳打一行日志；调用方保证不把 Cookie / 代理值传进来。"""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def safe_url(url: str) -> str:
    """去掉 query 和 fragment，防止把 wzws_cid / token 打进日志。"""
    try:
        parts = urlsplit(url or "")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except (ValueError, TypeError):
        return "(无法解析的 URL)"


def parse_args() -> argparse.Namespace:
    """解析命令行参数；--proxy 缺省从 POJIE_PROXY 环境变量取。"""
    parser = argparse.ArgumentParser(
        description="吾爱破解瑞数（WZWS）真浏览器 PoC：默认只读验证，不执行签到提交。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("POJIE_PROXY", ""),
        help="可选代理，如 socks5://host:port；也可设环境变量 POJIE_PROXY（值不会被打印）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="显式给定才执行签到提交（有风控风险）；默认 False，只做只读验证",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        default=False,
        help="有头模式，需外部 Xvfb，例如 xvfb-run -a python probe.py --headed",
    )
    parser.add_argument(
        "--max-wait",
        type=int,
        default=40,
        help="等待挑战自动通过的秒数上限",
    )
    parser.add_argument(
        "--out",
        default="./shots",
        help="截图输出目录（只存图片，不打印 URL 参数）",
    )
    args = parser.parse_args()
    if args.max_wait < 1:
        parser.error("--max-wait 至少为 1 秒")
    return args


def enforce_rate_limit(profile_dir: Path) -> None:
    """最小间隔 + 每日上限。防的是反复撞主号，不做任何循环刷新。"""
    last_marker = profile_dir / ".last_run"
    now = time.time()
    if last_marker.exists():
        try:
            gap = now - float(last_marker.read_text(encoding="ascii").strip())
            if gap < MIN_INTERVAL_SECONDS:
                raise SystemExit(
                    f"风控保护：距离上次运行仅 {gap:.0f}s，"
                    f"请至少间隔 {MIN_INTERVAL_SECONDS}s 再跑（不要循环刷新）"
                )
        except (ValueError, OSError):
            pass

    day_marker = profile_dir / f".runs_{time.strftime('%Y%m%d')}"
    runs = 0
    if day_marker.exists():
        try:
            runs = int(day_marker.read_text(encoding="ascii").strip())
        except (ValueError, OSError):
            runs = 0
    if runs >= MAX_RUNS_PER_DAY:
        raise SystemExit(
            f"风控保护：今天已运行 {runs} 次，达到上限 {MAX_RUNS_PER_DAY} 次，请明天再试"
        )

    last_marker.write_text(str(now), encoding="ascii")
    day_marker.write_text(str(runs + 1), encoding="ascii")


# --------------------------------------------------------------------------- #
# 浏览器上下文
# --------------------------------------------------------------------------- #
def build_context(pw, stealth_module, profile_dir: Path, args: argparse.Namespace):
    """用 persistent context 复用 profile；返回 (context, stealth 是否已自动注入)。"""
    kwargs = {
        "user_data_dir": str(profile_dir),
        "headless": not args.headed,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ],
        "viewport": {"width": 1366, "height": 768},
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "user_agent": USER_AGENT,
    }
    if args.proxy:
        # Playwright 的 socks5:// 默认走远端 DNS，等价于 socks5h。
        kwargs["proxy"] = {"server": args.proxy}

    if stealth_module is not None and hasattr(stealth_module, "Stealth"):
        # playwright-stealth 1.x：launch_persistent_context 会给每个页面自动打补丁。
        try:
            context = stealth_module.Stealth().launch_persistent_context(**kwargs)
            return context, True
        except Exception as exc:  # noqa: BLE001 - 版本 API 不一致时回退普通模式
            log(f"stealth 持久化上下文初始化失败，回退普通模式：{type(exc).__name__}")

    context = pw.chromium.launch_persistent_context(**kwargs)
    return context, False


def preload_cookies(context, raw_cookie: str) -> int:
    """把 POJIE_COOKIE（整行 Cookie 头）拆成 Playwright cookie 列表；不打印值。"""
    cookies = []
    for part in (raw_cookie or "").split(";"):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if not name:
            continue
        cookies.append(
            {"name": name, "value": value.strip(), "domain": COOKIE_DOMAIN, "path": "/"}
        )
    if cookies:
        context.add_cookies(cookies)
    return len(cookies)


# --------------------------------------------------------------------------- #
# 页面状态检测
# --------------------------------------------------------------------------- #
def inspect(page) -> dict:
    """只读地把当前页面状态摘出来；html 只用于内部判断，绝不返回/打印正文。"""
    state = {
        "title": "",
        "url": "",
        "passed": False,
        "challenge": False,
        "login": False,
        "sign_entry": False,
        "html_len": 0,
    }
    try:
        state["title"] = (page.title() or "").strip()
    except Exception:  # noqa: BLE001 - 页面正在跳转时取不到，按空处理
        pass
    try:
        state["url"] = page.url or ""
    except Exception:  # noqa: BLE001
        pass

    try:
        html = page.content() or ""
    except Exception:  # noqa: BLE001
        html = ""
    state["html_len"] = len(html)
    lowered = html.lower()
    title_lower = state["title"].lower()

    state["challenge"] = any(mark in lowered for mark in CHALLENGE_MARKERS)
    title_hit = any(mark in title_lower for mark in FORUM_TITLE_MARKERS)
    body_hit = any(mark in lowered for mark in FORUM_BODY_MARKERS)
    # 挑战页没有正常 title 且带 wzws 特征；必须两者都不像挑战才算真过去。
    state["passed"] = (title_hit or body_hit) and not state["challenge"]

    # 登录态：优先 DOM，正文文字兜底（只判断，不打印命中内容）。
    try:
        state["login"] = (
            page.locator(".vwmy").count() > 0
            or page.locator('a[href*="action=logout"]').count() > 0
            or page.locator('a:has-text("退出")').count() > 0
        )
    except Exception:  # noqa: BLE001
        state["login"] = "退出" in html or "logout" in lowered
    if not state["login"]:
        # 兜底只在 DOM 没命中时生效，且不能把 DOM 已经判定的登录态又抹掉。
        has_guest_hint = any(hint in html for hint in LOGIN_HINTS)
        state["login"] = body_hit and not has_guest_hint

    # 签到入口：erling_qd 插件 / Discuz 任务链接 / 写着「签到」的链接。
    try:
        state["sign_entry"] = (
            page.locator('a[href*="erling_qd"]').count() > 0
            or page.locator('a[href*="mod=task"]').count() > 0
            or page.locator('a:has-text("签到")').count() > 0
        )
    except Exception:  # noqa: BLE001
        state["sign_entry"] = "erling_qd" in lowered or "签到" in html
    return state


def maybe_click_challenge(page) -> str:
    """瑞数主要靠 JS 自动完成，通常没有勾选框；只在明确看到控件时轻点一次。"""
    selectors = (
        'iframe[title*="Turnstile"]',
        'iframe[src*="challenge"]',
        ".g-recaptcha",
        "#captcha",
        ".wzws-verify",
        "text=点击验证",
        "text=请点击",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click(timeout=2000)
                return selector
        except Exception:  # noqa: BLE001 - 控件消失/不可点就换下一个
            continue
    return ""


def wait_for_challenge(page, max_wait: int, out_dir: Path) -> dict:
    """分阶段等待：前若干秒纯等待不交互，之后才考虑点一下挑战控件。"""
    started = time.monotonic()
    challenge_shot = out_dir / "01-challenge.png"
    passed_shot = out_dir / "02-passed.png"
    saved_challenge = False
    pure_wait = min(8, max_wait)
    clicked = ""

    while True:
        elapsed = time.monotonic() - started
        state = inspect(page)

        if elapsed >= 1.0 and not saved_challenge:
            try:
                page.screenshot(path=str(challenge_shot), full_page=False)
                saved_challenge = True
            except Exception as exc:  # noqa: BLE001
                log(f"挑战页截图失败（不影响判定）：{type(exc).__name__}")

        if state["passed"]:
            try:
                page.screenshot(path=str(passed_shot), full_page=False)
            except Exception as exc:  # noqa: BLE001
                log(f"通过后截图失败（不影响判定）：{type(exc).__name__}")
            return {
                "passed": True,
                "elapsed": elapsed,
                "clicked": clicked,
                "challenge_shot": challenge_shot if saved_challenge else None,
                "passed_shot": passed_shot,
                "state": state,
            }

        if elapsed >= max_wait:
            break
        if elapsed >= pure_wait and not clicked:
            clicked = maybe_click_challenge(page)
            if clicked:
                log("检测到挑战控件，已点击一次（瑞数通常不需要这一步）")
        page.wait_for_timeout(1000)

    return {
        "passed": False,
        "elapsed": time.monotonic() - started,
        "clicked": clicked,
        "challenge_shot": challenge_shot if saved_challenge else None,
        "passed_shot": None,
        "state": state,
    }


# --------------------------------------------------------------------------- #
# 签到（仅 --apply）
# --------------------------------------------------------------------------- #
def extract_message(page) -> str:
    """读 Discuz 的 #messagetext 提示框，读不到返回空串；不打印整页正文。"""
    try:
        locator = page.locator("#messagetext")
        if locator.count() == 0:
            return ""
        text = locator.first.inner_text(timeout=3000)
    except Exception:  # noqa: BLE001
        return ""
    match = MESSAGE_RE.search(text) if "<" in text else None
    if match:
        text = re.sub(r"<[^>]+>", " ", match.group(1))
    return re.sub(r"\s+", " ", text or "").strip()


def do_apply_once(page) -> tuple[str, str]:
    """复用现有 pojie.py 的 apply -> draw 思路，各访问一次，绝不重试。"""
    messages: list[str] = []
    for label, url in (("申请", APPLY_URL), ("领奖", DRAW_URL)):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(800)
            message = extract_message(page)
        except Exception as exc:  # noqa: BLE001
            message = f"(读取失败：{type(exc).__name__})"
        messages.append(message)
        log(f"{label}提示：{message[:120] or '（空）'}")

    both = " ".join(messages)
    if any(hint in both for hint in NO_TASK_HINTS):
        return "no_task", both[:160]
    if any(hint in both for hint in LOGIN_HINTS):
        return "login", both[:160]
    if any(hint in both for hint in OK_HINTS):
        return "ok", both[:160]
    if any(hint in both for hint in DONE_HINTS):
        return "done", both[:160]
    return "unknown", both[:160]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> None:
    args = parse_args()

    # 重型依赖延迟导入：保证没有 Playwright 的机器上 --help 依然能用。
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise SystemExit(
            "未安装 playwright：本脚本设计为在 browser-poc 容器里运行，见 README 的构建步骤。"
        ) from exc
    try:
        # 导入模块本身（不是 Stealth 类），这样既能用新版 Stealth().launch_persistent_context，
        # 也能在老版上回退到模块级的 stealth_sync(page)。
        import playwright_stealth as stealth_module  # noqa: F401
    except ImportError:  # pragma: no cover - stealth 是加分项，缺了也能跑出结论
        stealth_module = None

    here = Path(__file__).resolve().parent
    profile_dir = here / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    enforce_rate_limit(profile_dir)

    raw_cookie = os.environ.get("POJIE_COOKIE", "")
    log(f"模式：{'headed + Xvfb' if args.headed else 'headless'}；"
        f"签到提交：{'开启（--apply，有风控风险）' if args.apply else '关闭（只读）'}")
    log(f"代理：{'已配置（值不显示）' if args.proxy else '未配置'}；"
        f"Cookie：{'已注入（值不显示）' if raw_cookie else '未注入'}")

    total_started = time.monotonic()
    with sync_playwright() as pw:
        context = None
        try:
            context, stealth_applied = build_context(pw, stealth_module, profile_dir, args)
            if raw_cookie:
                count = preload_cookies(context, raw_cookie)
                log(f"已预置 {count} 条 Cookie，浏览器会自行重算短时效 wzws_cid")
            if not stealth_applied and stealth_module is not None:
                # 老版 playwright-stealth 没有 launch_persistent_context，退化成页面级补丁。
                log("stealth 走页面级兼容模式")
            page = context.pages[0] if context.pages else context.new_page()
            if not stealth_applied and hasattr(stealth_module, "stealth_sync"):
                stealth_module.stealth_sync(page)

            try:
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                log(f"打开首页失败：{type(exc).__name__} —— 检查网络/代理出口")

            result = wait_for_challenge(page, args.max_wait, out_dir)
            state = result["state"]
            total = time.monotonic() - total_started

            print("===== 只读验证报告（默认未执行签到提交）=====", flush=True)
            print(f"是否通过挑战 : {'是' if result['passed'] else '否'}", flush=True)
            print(f"挑战等待耗时 : {result['elapsed']:.1f}s", flush=True)
            print(f"最终 URL     : {safe_url(state['url'])}", flush=True)
            print(f"页面 title   : {state['title'] or '（空）'}", flush=True)
            print(f"是否登录态   : {'是' if state['login'] else '否'}", flush=True)
            print(f"是否签到入口 : {'是' if state['sign_entry'] else '否'}", flush=True)
            print(f"挑战页截图   : {result['challenge_shot'] or '（未生成）'}", flush=True)
            print(f"通过后截图   : {result['passed_shot'] or '（未生成）'}", flush=True)
            print(f"总耗时       : {total:.1f}s", flush=True)
            print("==============================================", flush=True)

            if args.apply:
                log("警告：--apply 已开启，即将执行签到提交（只做一次）")
                if not state["login"]:
                    log("警告：未检测到登录态，签到很可能失败；确认 POJIE_COOKIE 已设置")
                apply_state, apply_message = do_apply_once(page)
                verdict = {
                    "ok": "签到成功",
                    "done": "今日已签到 / 任务已完成",
                    "login": "登录态失效",
                    "no_task": "签到任务不存在（id 可能改了）",
                    "unknown": "结果无法判断，请手动去站点确认",
                }.get(apply_state, apply_state)
                log(f"签到结论：{verdict}｜{apply_message[:120]}")
            elif result["passed"]:
                log("只读验证完成：确认能过挑战后，再决定是否用 --apply 跑一次签到。")
            else:
                log("只读验证完成：本次未见论坛特征，挑战可能没过；见 README 排查章节。")
        finally:
            # 必须显式关闭 persistent context，避免 greenlet / SIGALRM 留下孤儿 Chromium。
            # close 必须在 sync_playwright() 上下文内调用：驱动退出后就关不掉了。
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass


if __name__ == "__main__":
    main()
