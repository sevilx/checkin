"""LinuxDo（linux.do）：登录校验 + 真实统计 + 可选的阅读上报 / 点赞。

linux.do 是 Discourse 论坛，**没有传统意义上的"签到按钮"**。本模块做的是：

1. 用 `/session/current.json` 校验登录态；
2. 从 `/u/<用户名>.json` 读出真实统计（阅读时长、已读帖数、活跃天数、点赞/获赞）
   —— 这是判断离 TL3 还差多少的唯一可靠口径，比只显示 TL 有用；
3. 可选：随机读几个主题，把**实际耗时**按楼层上报给 `/topics/timings`；
4. 可选：对本次读过的帖子点赞，带配额和间隔。

第 3、4 项默认关闭，必须显式打开。原因写在 README 的 LinuxDo 章节里：
Discourse 服务端对单批 timings 只做 `MAX_READ_TIME_PER_BATCH = 60_000` 的上限裁剪，
不校验"人不可能读这么快"，所以伪造时长在技术上完全可行 —— 本模块刻意不提供这个能力，
只按真实耗时上报，宁可涨得慢，也不把签到工具做成刷等级工具。

403 有四种完全不同的原因，必须分开说，否则用户会一直换 cookie：
* 站点回的是 Cloudflare 挑战页 → 出口 IP / TLS 指纹的问题（已用 curl_cffi 多指纹兜底）；
* Cookie 里没有 `_t` → Discourse 的登录令牌是 HttpOnly，控制台 document.cookie 拿不到，
  只能从「网络 → 请求标头 → Cookie」整行复制；
* Cookie 里有 `_t` 但站点不认 → 那是别的环境签发的，重新登录一次；
* 出口 IP 被 linux.do 的风控盯上 → 换节点。
"""

from __future__ import annotations

import random
import re
import time

from ..core import Line, Site, XHR_HEADERS, looks_blocked, parse_cookie

BASE = "https://linux.do"
MAX_BROWSE = 10
MAX_LIKE = 5
# 低于这个耗时的主题不报。实测家宽 + 代理拉一个主题 json 约 0.5~1 秒，
# 门槛设太高一整天都不会触发（开关就白开了），所以按"确实花掉了这些时间"取 400ms。
MIN_REPORT_MS = 400


def _int(value, default: int = 0, lo: int = 0, hi: int = 0) -> int:
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if num < lo:
        return lo
    if hi and num > hi:
        return hi
    return num


def _flag(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _duration(seconds: int) -> str:
    hours, mins = divmod(seconds // 60, 60)
    if hours:
        return f"{hours} 小时 {mins} 分"
    return f"{max(mins, 1)} 分钟"


def _cf_blocked(resp) -> bool:
    """401/403/404 里区分「Cloudflare 挑战页」和「站点正常拒绝」。

    core.looks_blocked() 已覆盖 cf-mitigated: challenge 与高特异性正文标记（对 404 也生效）。
    这里再补一层响应头兜底：CF 允许把拦截页自定义成 404，但正常走 CF 的 Discourse 404
    也会带 cf-ray / server: cloudflare，所以这两个头不能单独当判据 —— 正文一眼是
    正常未登录 JSON（current_user / error_type）时就按站点正常拒绝处理。
    """
    if looks_blocked(
        getattr(resp, "status_code", 0),
        getattr(resp, "text", "") or "",
        getattr(resp, "headers", None),
    ):
        return True
    headers = getattr(resp, "headers", None) or {}
    try:
        ray = str(headers.get("cf-ray") or "")
        server = str(headers.get("server") or "").lower()
        mitigated = str(headers.get("cf-mitigated") or "")
    except Exception:  # noqa: BLE001
        ray, server, mitigated = "", "", ""
    body = (getattr(resp, "text", "") or "")[:4000].lower()
    if "current_user" in body or "error_type" in body:
        return False
    return bool(ray or mitigated) or "cloudflare" in server


def _cf_block_message(status: int) -> str:
    """与挑战性质匹配的可操作提示（复用 core.cf_hint 的思路，但不说 _t 失效）。"""
    return (
        f"登录校验被 Cloudflare / WAF 挑战（HTTP {status}，CF 可能用 404 当拦截页）。"
        "这不是登录令牌失效，别反复重登 cookie：\n"
        "1) 出口 IP 最要紧：给这个站点配一条和浏览器同一个出口（同一节点、同一公网 IP）的代理，"
        "节点不干净就换一个；\n"
        "2) 不要把浏览器里的 cf_clearance 抄进来：它和当时的出口 IP、UA 绑定，换节点必然失效，"
        "本程序默认已经帮你丢掉；\n"
        "3) 容器里代理可能没生效，可用 tools/diagnose_proxy.py 先看连通性和出口 IP。"
    )


class LinuxDo(Site):
    key = "LINUXDO"
    name = "LinuxDo 论坛"
    fields = ("cookie", "browse", "read_report", "like", "keep_cf", "ua")
    doc = (
        "登录 linux.do → F12 → 网络 → 任意请求 → 请求标头里的 Cookie 整行复制"
        "（必须有 _t，它是 HttpOnly，控制台 document.cookie 里没有）。"
        f"browse=浏览主题数（默认 3，上限 {MAX_BROWSE}）；"
        "read_report=1 时把实际阅读耗时上报（默认关）；"
        f"like=N 时对本次读过的帖子点赞（默认关，上限 {MAX_LIKE}）；"
        "keep_cf=1 时保留浏览器里的 cf_clearance（默认丢弃），"
        "必须同时把当时那个浏览器的 UA 填进 ua，且容器出口 IP 要和浏览器一致——"
        "三项对不上就别开，会更像伪造请求"
    )
    cf_impersonates = ("safari15_5", "chrome120", "chrome110")

    def __init__(self, account: dict, index: int = 0):
        super().__init__(account, index)
        cookies = parse_cookie(self.cookie)
        self.has_token = bool(cookies.get("_t"))
        if not self.has_token and not cookies.get("_forum_session"):
            raise ValueError(
                "Cookie 里既没有 _t 也没有 _forum_session，复制到的不是 linux.do 的 Cookie："
                "F12 → 网络 → 点任意请求 → 请求标头 → 把 Cookie 整行复制过来"
            )

    def _headers(self, kept_cf: bool) -> dict:
        return self.cf_headers(
            {
                **XHR_HEADERS,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}/",
                "Origin": BASE,
            },
            kept_cf,
        )

    # ------------------------------------------------------------------ #
    def run(self) -> list[Line]:
        cookie_str, kept_cf = self.cf_cookies()
        headers = self._headers(kept_cf)

        current, session = self.browser_request(
            lambda sess: sess.get(f"{BASE}/session/current.json", timeout=25),
            cookie=cookie_str,
            headers=headers,
        )
        self._guard(current)

        try:
            user = (current.json() or {}).get("current_user") or {}
        except ValueError:
            raise RuntimeError("登录校验返回非 JSON：站点返回的不像接口数据，可能被拦截了") from None

        if not user.get("username"):
            raise RuntimeError("Cookie 已失效（接口能访问，但没有登录态），重新登录后复制一次完整 Cookie")

        username = str(user["username"])
        uid = user.get("id")
        lines = [
            Line("账号", username),
            Line("信任等级", f"TL{user.get('trust_level', '?')}"),
        ]

        stats = self._stats_lines(session, headers, username)
        lines.extend(stats)

        browse = _int(self.account.get("browse"), 3, 0, MAX_BROWSE)
        report = _flag(self.account.get("read_report"))
        like_quota = _int(self.account.get("like"), 0, 0, MAX_LIKE)

        if browse or like_quota:
            csrf = self._csrf(session, headers) if (report or like_quota) else ""
            lines.extend(
                self._browse(
                    session, headers, csrf,
                    browse=browse, report=report, like_quota=like_quota,
                    uid=uid, username=username,
                )
            )
        elif report:
            lines.append(Line("阅读上报", "未开启浏览，无内容可报（把 browse 设为 >0）"))

        # 统计拿不到时退回到 Connect 页：至少告诉用户去哪里看 TL3 进度
        if not stats:
            try:
                connect = session.get("https://connect.linux.do/", timeout=25)
                if connect.status_code == 200 and "信任级别" in connect.text:
                    lines.append(Line("Connect", "信息页可访问（信任等级进度见网页）"))
            except Exception:  # noqa: BLE001
                pass

        return lines

    # ------------------------------------------------------------------ #
    # 浏览 / 上报 / 点赞
    # ------------------------------------------------------------------ #
    def _browse(
        self, session, headers, csrf: str, *,
        browse: int, report: bool, like_quota: int, uid, username: str,
    ) -> list[Line]:
        topics = self._latest(session, headers)
        lines: list[Line] = []
        if not topics:
            if browse:
                lines.append(Line("浏览主题", "列表拉不到，跳过"))
            return lines

        picked = random.sample(topics, min(browse, len(topics))) if browse else topics[: like_quota]
        viewed = 0
        reported = 0
        report_failed = ""
        liked = 0
        like_note = ""
        like_stopped = False

        for topic in picked:
            tid = topic.get("id")
            if not tid:
                continue

            started = time.monotonic()
            try:
                detail = session.get(f"{BASE}/t/{tid}.json", timeout=25)
            except Exception:  # noqa: BLE001
                continue
            if detail.status_code != 200:
                continue
            elapsed_ms = int((time.monotonic() - started) * 1000)
            viewed += 1
            try:
                body = detail.json() or {}
            except ValueError:
                continue

            if report and csrf and elapsed_ms >= MIN_REPORT_MS:
                ok, why = self._report(session, headers, csrf, tid, body, elapsed_ms)
                if ok:
                    reported += 1
                elif why and not report_failed:
                    report_failed = why

            if like_quota and csrf and not like_stopped and liked < like_quota:
                posts = ((body.get("post_stream") or {}).get("posts")) or []
                for post in posts:
                    if liked >= like_quota:
                        break
                    if not self._likeable(post, uid):
                        continue
                    time.sleep(random.uniform(1.5, 3.0))
                    ok, why = self._like(session, headers, csrf, post["id"])
                    if ok:
                        liked += 1
                    elif why == "rate":
                        like_note = "，被限流（429）已停止"
                        like_stopped = True
                        break

        if browse:
            lines.append(Line("浏览主题", f"{viewed}/{len(picked)}"))
        if report:
            if reported:
                lines.append(Line("阅读上报", f"{reported} 个主题（按实际耗时，未伪造）"))
            else:
                lines.append(
                    Line("阅读上报", report_failed or f"本次没有够时长（≥{MIN_REPORT_MS / 1000:g} 秒）的主题")
                )
        if like_quota:
            if liked:
                lines.append(Line("点赞", f"{liked}/{like_quota}{like_note}"))
            else:
                lines.append(Line("点赞", like_note.lstrip("，") or "没找到可点赞的新帖"))
        return lines

    def _latest(self, session, headers) -> list[dict]:
        try:
            resp = session.get(f"{BASE}/latest.json?order=default", timeout=25)
            if resp.status_code != 200:
                return []
            return ((resp.json() or {}).get("topic_list") or {}).get("topics") or []
        except Exception:  # noqa: BLE001
            return []

    def _csrf(self, session, headers) -> str:
        """`/session/csrf.json` 匿名也能拿到 token，不用去 HTML 里抠 meta。"""
        try:
            resp = session.get(f"{BASE}/session/csrf.json", timeout=20)
            if resp.status_code == 200:
                token = (resp.json() or {}).get("csrf") or ""
                if token:
                    return str(token)
        except Exception:  # noqa: BLE001
            pass
        try:  # 兜底：从首页的 meta 标签里抠（老版本 Discourse 才需要）
            resp = session.get(f"{BASE}/", timeout=20)
            match = re.search(r'name="csrf-token"\s+content="([^"]+)"', resp.text or "")
            if match:
                return match.group(1)
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _report(self, session, headers, csrf: str, topic_id, body: dict, elapsed_ms: int) -> tuple[bool, str]:
        """按实际耗时上报。总时长就是这次 GET 花掉的时间，按楼层均分，不做任何放大。"""
        posts = [
            post for post in ((body.get("post_stream") or {}).get("posts") or [])
            if _int(post.get("post_number"), 0) >= 1
        ]
        if not posts:
            return False, ""
        share = max(1, elapsed_ms // len(posts))
        data = {"topic_id": str(topic_id), "topic_time": str(share * len(posts))}
        for post in posts:
            data[f"timings[{post['post_number']}]"] = str(share)
        try:
            resp = session.post(
                f"{BASE}/topics/timings",
                data=data,
                headers={
                    **headers,
                    "X-CSRF-Token": csrf,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                timeout=20,
            )
        except Exception:  # noqa: BLE001
            return False, "上报请求失败"
        if resp.status_code in (200, 204):
            return True, ""
        if resp.status_code in (403, 422):
            return False, "上报被拒（CSRF 或登录态失效）"
        return False, f"上报失败 HTTP {resp.status_code}"

    @staticmethod
    def _likeable(post: dict, uid) -> bool:
        if not post.get("id"):
            return False
        if uid and post.get("user_id") == uid:
            return False
        summary = post.get("actions_summary") or []
        acted = next((item for item in summary if item.get("id") == 2), None)
        if acted and acted.get("acted"):
            return False
        return not post.get("current_user_reaction")

    def _like(self, session, headers, csrf: str, post_id) -> tuple[bool, str]:
        """点赞走 discourse-reactions 插件；老版本退回 /post_actions。"""
        common = {
            **headers,
            "X-CSRF-Token": csrf,
            "Accept": "application/json",
            "Referer": f"{BASE}/",
        }
        try:
            resp = session.put(
                f"{BASE}/discourse-reactions/posts/{post_id}/custom-reactions/heart/toggle.json",
                data="{}",
                headers={**common, "Content-Type": "application/json"},
                timeout=20,
            )
        except Exception:  # noqa: BLE001
            return False, "net"
        if resp.status_code in (200, 201, 204):
            return True, ""
        if resp.status_code == 429:
            return False, "rate"
        if resp.status_code == 404:
            return self._like_legacy(session, common, post_id)
        if resp.status_code in (403, 422):
            return False, "deny"
        return False, f"http{resp.status_code}"

    def _like_legacy(self, session, headers, post_id) -> tuple[bool, str]:
        try:
            resp = session.post(
                f"{BASE}/post_actions",
                data={"id": str(post_id), "post_action_type_id": "2"},
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=20,
            )
        except Exception:  # noqa: BLE001
            return False, "net"
        if resp.status_code in (200, 201, 204):
            return True, ""
        if resp.status_code == 429:
            return False, "rate"
        return False, f"http{resp.status_code}"

    # ------------------------------------------------------------------ #
    def _stats_lines(self, session, headers, username: str) -> list[Line]:
        """真实统计。拿不到就返回空列表，不影响签到结论。

        数据在 `/u/<用户名>/summary.json` 的 `user_summary` 里。
        注意 `/u/<用户名>.json` 的 `user_stat` 在 linux.do 上是 **null**，
        只有一个空的 user 对象，所以那个接口只能当兜底（别的 Discourse 实例会用）。
        """
        stat: dict = {}
        try:
            resp = session.get(f"{BASE}/u/{username}/summary.json", timeout=25)
            if resp.status_code == 200:
                stat = (resp.json() or {}).get("user_summary") or {}
        except Exception:  # noqa: BLE001
            stat = {}
        if not stat:
            try:
                resp = session.get(f"{BASE}/u/{username}.json", timeout=25)
                if resp.status_code == 200:
                    stat = ((resp.json() or {}).get("user") or {}).get("user_stat") or {}
            except Exception:  # noqa: BLE001
                stat = {}
        if not stat:
            return []

        out: list[Line] = []
        seconds = _int(stat.get("time_read"), 0)
        if seconds:
            out.append(Line("阅读时长", _duration(seconds)))
        posts = _int(stat.get("posts_read_count"), 0)
        if posts:
            out.append(Line("已读帖数", f"{posts} 篇"))
        days = _int(stat.get("days_visited"), 0)
        if days:
            out.append(Line("活跃天数", f"{days} 天"))
        given = _int(stat.get("likes_given"), 0)
        got = _int(stat.get("likes_received"), 0)
        if given or got:
            out.append(Line("点赞/获赞", f"{given} / {got}"))
        return out

    # ------------------------------------------------------------------ #
    def _guard(self, resp) -> None:
        """把非 200 的结果翻译成能照做的动作；风控拦截已由 browser_request 提前抛掉。

        403/404 不能一律说成 "_t 失效"：Discourse 对未登录的 /session/current.json
        本来就回 404，而挂在 Cloudflare 后的 linux.do 在出口 IP 被挑战时，接口也会
        回 403/404。真正 "_t 失效" 只是其中一种可能，得先看是不是出口/凭证的问题，
        否则用户会反复重登 cookie，问题却一直不解决。
        """
        if resp.status_code == 200:
            return
        if resp.status_code in (401, 403, 404):
            # 双保险：A 层（browser_request / looks_blocked）通常在 404 挑战页处就换指纹
            # 并抛 cf_hint 了；万一漏判，这里按响应头 / 正文再判一次，
            # 别把出口 IP 被挑战说成 "_t 失效"。
            if _cf_blocked(resp):
                raise RuntimeError(_cf_block_message(resp.status_code))
            # Discourse 对未登录的 /session/current.json 回 404，不是"接口不存在"
            if not self.has_token:
                raise RuntimeError(
                    "Cookie 里没有 _t（Discourse 的登录令牌）：它是 HttpOnly，"
                    "控制台里输 document.cookie 是拿不到的。"
                    "请用 F12 → 网络 → 任意请求 → 请求标头 → Cookie 整行复制。"
                )
            # 有 _t 却被拒：优先怀疑出口 IP / CF 会话，而不是又让用户去重登。
            # 只有当确实没有 cf_clearance 可用、且出口这一层已排除时，才指向 _t 本身。
            raise RuntimeError(
                f"登录校验被拒（HTTP {resp.status_code}）。有 _t 但接口不认，按可能性从高到低排查：\n"
                "1) 出口 IP：如果你在 NAS 上走了代理 / 机场节点，Cloudflare 会认出口——"
                "先给这个站点配一条和浏览器同一个出口（同一节点、同一公网 IP）的代理再试；\n"
                "2) CF 会话：浏览器里 cf_clearance 是在你的出口换来的，默认已被本程序丢掉。"
                "若确认容器和浏览器同出口，可在设置里把 keep_cf=1 打开，并把当时那个浏览器的 UA 填进 ua"
                "（两者必须配对；出口不一致时千万别开，会更像伪造）；\n"
                "3) 排除以上两步后仍不行，才是 _t 本身失效：改密码 / 手动退出 / 换设备都会让它失效，"
                "重新登录一次再复制完整 Cookie。"
            )
        raise RuntimeError(f"登录校验失败 HTTP {resp.status_code}")

    def verify(self) -> tuple[bool, str]:
        """只读检测：Discourse 的 /session/current.json，返回 current_user 即有效。"""
        cookie_str, kept_cf = self.cf_cookies()
        resp, _ = self.browser_request(
            lambda sess: sess.get(f"{BASE}/session/current.json", timeout=20),
            cookie=cookie_str,
            headers=self._headers(kept_cf),
        )
        if resp.status_code == 200:
            try:
                user = resp.json().get("current_user") or {}
            except ValueError:
                return False, "返回非 JSON，可能被拦截"
            if user.get("username"):
                return True, f"Cookie 有效：{user['username']}（信任等级 {user.get('trust_level', '?')}）"
            return False, "接口能访问但没有登录态，Cookie 已失效"
        if resp.status_code in (401, 403, 404):
            # 和 _guard 同样的双保险：CF 自定义 404 挑战页不能说成 cookie 失效
            if _cf_blocked(resp):
                return False, _cf_block_message(resp.status_code)
            return False, "Cookie 已失效（未登录）"
        return False, f"检测失败：HTTP {resp.status_code}"
