"""LinuxDo CF 挑战修复的桩测试（只依赖标准库，不联网、不读真实凭据）。

运行：
    python -B -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from checkin import core
from checkin.sites.linuxdo import LinuxDo

FF_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:160.0) "
    "Gecko/20100101 Firefox/160.0"
)


class StubResponse:
    """最小响应桩：只需要 status_code / text / headers 三个属性。"""

    def __init__(self, status_code: int = 200, text: str = "", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def make_site(ua: str = FF_UA, keep_cf: str = "1") -> LinuxDo:
    """用假 cookie 构造站点实例，绝不接触真实凭据。"""
    return LinuxDo({"cookie": "_t=FAKE; cf_clearance=FAKE", "ua": ua, "keep_cf": keep_cf})


class TestCfChallenge404(unittest.TestCase):
    """今天失败的直接原因：404 + cf-ray + 空 body 必须判为挑战并触发轮换。"""

    def test_404_cf_ray_empty_body_is_blocked(self):
        site = make_site()
        resp = StubResponse(404, "", {"cf-ray": "abc-def", "server": "cloudflare"})
        self.assertTrue(site.is_blocked(resp))

    def test_404_server_cloudflare_only_is_blocked(self):
        site = make_site()
        resp = StubResponse(404, "", {"server": "cloudflare"})
        self.assertTrue(site.is_blocked(resp))

    def test_discourse_404_json_is_not_blocked(self):
        site = make_site()
        body = '{"current_user": null, "error_type": "not_logged_in"}'
        resp = StubResponse(404, body, {"cf-ray": "x", "server": "cloudflare"})
        self.assertFalse(site.is_blocked(resp))

    def test_plain_404_without_cf_headers_is_not_blocked(self):
        site = make_site()
        self.assertFalse(site.is_blocked(StubResponse(404, "not found", {})))

    def test_core_predicate_unchanged_for_other_sites(self):
        """core.looks_blocked 判据未改：404+cf-ray 在基类仍不算拦截。"""
        resp = StubResponse(404, "", {"cf-ray": "x", "server": "cloudflare"})
        self.assertFalse(core.looks_blocked(404, "", resp.headers))
        base = core.Site({"cookie": "a=b"})
        self.assertFalse(base.is_blocked(resp))

    def test_browser_request_rotates_on_cf_404(self):
        """404 挑战必须换下一个指纹，而不是第一个指纹失败就抛错。"""
        saved = core.cffi_requests
        core.cffi_requests = object()
        try:
            site = make_site()
            calls: list[str | None] = []

            class First:
                def get(self, url, **kwargs):
                    return StubResponse(404, "", {"cf-ray": "x", "server": "cloudflare"})

            class Second:
                def get(self, url, **kwargs):
                    return StubResponse(200, "{}")

            def fake_session(headers=None, cookie=None, browser=None):
                calls.append(browser)
                return First() if browser == "firefox155" else Second()

            site.session = fake_session
            resp, _ = site.browser_request(lambda sess: sess.get("https://example.test"))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(calls, ["firefox155", "firefox154"])
        finally:
            core.cffi_requests = saved


class TestFingerprints(unittest.TestCase):
    """Firefox 家族纯净化 + client hints 裁剪。"""

    def setUp(self):
        self._saved = core.cffi_requests
        core.cffi_requests = object()  # 模拟已安装 curl_cffi，impersonates() 走指纹分支

    def tearDown(self):
        core.cffi_requests = self._saved

    def test_firefox_branch_has_no_chrome(self):
        actual = make_site().impersonates()
        self.assertEqual(
            actual,
            ("firefox155", "firefox154", "firefox153", "firefox144", "firefox133"),
        )
        self.assertFalse(any("chrome" in name for name in actual))

    def test_chrome_branch_unchanged(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0.0.0 Safari/537.36"
        self.assertEqual(
            make_site(ua=ua).impersonates(),
            ("chrome120", "chrome110", "firefox155"),
        )

    def test_no_ua_keeps_original_rotation(self):
        self.assertEqual(
            make_site(ua="").impersonates(),
            ("safari15_5", "chrome120", "chrome110"),
        )

    def test_sec_ch_dropped_for_non_chromium(self):
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Sec-CH-UA": '"Chromium";v="141"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
        }
        for browser in ("firefox155", "safari15_5", "tor145"):
            with self.subTest(browser=browser):
                out = core._fingerprint_headers(headers, browser)
                self.assertFalse(any(k.lower().startswith("sec-ch-") for k in out))
                self.assertFalse(any(k.lower().startswith("sec-fetch-") for k in out))
                self.assertEqual(out["X-Requested-With"], "XMLHttpRequest")
        out_chrome = core._fingerprint_headers(headers, "chrome120")
        self.assertIn("Sec-Fetch-Dest", out_chrome)

    def test_curl_cffi_version_parsing(self):
        class Fake:
            __version__ = "0.17.0b1"

        core.cffi_requests = Fake
        self.assertEqual(core.curl_cffi_version(), (0, 17, 0))

        class Old:
            __version__ = "0.15.0"

        core.cffi_requests = Old
        self.assertEqual(core.curl_cffi_version(), (0, 15, 0))


class TestExceptionVisibility(unittest.TestCase):
    """抛异常的指纹必须出现在 tried 提示里；旧版本要给升级提示。"""

    def test_exception_fingerprint_shown_in_hint(self):
        saved = core.cffi_requests
        core.cffi_requests = object()
        try:
            class StubSession:
                def get(self, url, **kwargs):
                    return StubResponse(404, "Attention Required! Cloudflare", {"cf-ray": "x"})

            class Boom(core.Site):
                key = "BOOM"
                cf_impersonates = ("firefox155", "chrome120")

                def session(self, headers=None, cookie=None, browser=None):
                    if browser == "firefox155":
                        raise ValueError("unsupported fingerprint")
                    return StubSession()

            with self.assertRaises(RuntimeError) as ctx:
                Boom({"cookie": "a=b"}).browser_request(
                    lambda sess: sess.get("https://example.test")
                )
            message = str(ctx.exception)
            self.assertIn("firefox155→EXC:ValueError", message)
            self.assertIn("chrome120→HTTP 404", message)
        finally:
            core.cffi_requests = saved

    def test_old_version_note_in_hint(self):
        saved = core.cffi_requests

        class Old:
            __version__ = "0.15.0"

        core.cffi_requests = Old
        try:
            hint = core.Site({"cookie": "a=b"}).cf_hint([("firefox155", 404)])
            self.assertIn("curl_cffi 0.15.0 低于 0.17", hint)
        finally:
            core.cffi_requests = saved


class TestDiagnoseScript(unittest.TestCase):
    """诊断脚本的参数与脱敏边界（不联网）。"""

    def test_new_args_are_parsed(self):
        from tools import diagnose_proxy

        saved = sys.argv
        sys.argv = [
            "diagnose_proxy.py", "--check-login",
            "--impersonate", "firefox155", "--ua", "UA-TEST",
        ]
        try:
            args = diagnose_proxy._parse_args()
            self.assertTrue(args.check_login)
            self.assertEqual(args.impersonate, "firefox155")
            self.assertEqual(args.ua, "UA-TEST")
            self.assertEqual(diagnose_proxy.MATRIX_IMPERSONATES, ("firefox155", "chrome120"))
        finally:
            sys.argv = saved

    def test_safe_error_never_leaks_proxy_credentials(self):
        from tools import diagnose_proxy

        secret_url = "socks5://user:SECRET_VALUE@proxy.example.invalid:1080"
        self.assertEqual(
            diagnose_proxy._safe_error(RuntimeError(secret_url)), "RuntimeError"
        )


if __name__ == "__main__":
    unittest.main()
