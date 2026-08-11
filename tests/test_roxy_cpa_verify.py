# -*- coding: utf-8 -*-
"""roxy 驱动路径 CPA 落盘校验 + WARNING_BANNER session 兜底增强的单元测试。

背景：protocol 路径（run_codex_oauth）已接入 _verify_cpa_auth_landed；
roxy 两条 CPA 分支（run_roxy_codex_oauth / run_roxy_registration_and_codex one-shot）
此前只 _submit_cpa_callback + _save_cpa_local_record 就直接返回 success，产生假成功。
本测试 mock proto._verify_cpa_auth_landed，断言两条分支按落盘结果分支 success/failed。

R4：_fetch_chatgpt_session 对 WARNING_BANNER（无 accessToken）增加主动刷新策略；
one-shot 拿 AT 处增加 cookie 协议兜底（_fetch_chatgpt_session_via_cookies）。
"""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import roxy_codex_oauth
from core import roxy_registration


class FakeDriver:
    """最小 Selenium 风格 driver：current_url + execute_async_script + set_page_load_timeout。"""

    def __init__(self, current_url="https://chatgpt.com/chat", script_results=None):
        self._url = current_url
        self._script_results = list(script_results or [])
        self.set_page_load_timeout = Mock()
        self.set_script_timeout = Mock()
        self.quit = Mock()
        self.get_calls = []

    @property
    def current_url(self):
        return self._url

    def get(self, url):
        self._url = url
        self.get_calls.append(url)

    def execute_async_script(self, script):
        if self._script_results:
            return self._script_results.pop(0)
        return {"ok": True, "data": {}}


def _patched(patchers):
    """把 patch 列表合并进单个 ExitStack，返回 (stack, mocks)。"""
    stack = ExitStack()
    mocks = {}
    for p in patchers:
        mocks[p["name"]] = stack.enter_context(p["enter"](**p.get("kwargs", {})))
    return stack, mocks


class RoxyCodexOauthCpaVerifyTests(unittest.TestCase):
    """R3a：run_roxy_codex_oauth 的 CPA 分支应调用 _verify_cpa_auth_landed 并按结果分支。"""

    def _run_once(self, verify_result):
        callback_url = "http://localhost:1455/auth/callback?code=code123&state=state123"
        client = Mock()
        client.open_profile.return_value = SimpleNamespace(profile_id="p1", raw={})
        driver = Mock()
        driver.current_url = "https://chatgpt.com/chat"

        stack, mocks = _patched([
            {"name": "client", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "RoxyBrowserClient", "return_value": client}},
            {"name": "detect", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "_detect_browser_kind", "return_value": "Roxy"}},
            {"name": "build", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "_build_driver", "return_value": driver}},
            {"name": "center", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "_center_browser_window"}},
            {"name": "fill", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "_fill_email_and_otp"}},
            {"name": "delay", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "human_delay"}},
            {"name": "phone", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "_do_phone_verification_if_present"}},
            {"name": "consent", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "_finish_consent_workspace", "return_value": callback_url}},
            {"name": "source", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._codex_auth_url_source", "return_value": "cpa"}},
            {"name": "auth", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._request_cpa_authorize_url", "return_value": {
                    "state": "state123",
                    "auth_url": "https://auth.openai.com/oauth/authorize?state=state123"}}},
            {"name": "code", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._extract_code", "return_value": "code123"}},
            {"name": "submit", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._submit_cpa_callback", "return_value": {
                    "status": "ok", "message": "submitted"}}},
            {"name": "save", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._save_cpa_local_record", "return_value": "/tmp/receipt.json"}},
            {"name": "verify", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._verify_cpa_auth_landed", "return_value": verify_result}},
        ])
        try:
            result = roxy_codex_oauth._run_roxy_codex_oauth_once(
                "user@example.com",
                otp_provider=lambda *a, **k: "123456",
                force=True,
            )
        finally:
            stack.close()

        mocks["verify"].assert_called_once_with("user@example.com")
        return result

    def test_cpa_not_landed_returns_failed(self):
        result = self._run_once(verify_result=False)
        self.assertEqual(result.get("status"), "failed")
        self.assertIs(result.get("ok"), False)
        self.assertIn("未落盘可用 auth 文件", result.get("message") or "")
        self.assertEqual(result.get("email"), "user@example.com")

    def test_cpa_landed_returns_success(self):
        result = self._run_once(verify_result=True)
        self.assertEqual(result.get("status"), "success")
        self.assertIs(result.get("ok"), True)


class RoxyRegistrationOneShotCpaVerifyTests(unittest.TestCase):
    """R3b：run_roxy_registration_and_codex one-shot 的 CPA 提交处应调用校验并按结果分支。"""

    def _run_one_shot(self, verify_result):
        callback_url = "http://localhost:1455/auth/callback?code=code123&state=state123"
        client = Mock()
        client.open_profile.return_value = SimpleNamespace(profile_id="p1", raw={})
        driver = Mock()
        driver.current_url = "https://chatgpt.com/chat"

        stack, mocks = _patched([
            {"name": "client", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "RoxyBrowserClient", "return_value": client}},
            {"name": "build", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_build_driver", "return_value": driver}},
            {"name": "center", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_center_browser_window"}},
            {"name": "safe_get", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_safe_get"}},
            {"name": "warmup", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_page_warmup"}},
            {"name": "stop", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_check_manual_stop"}},
            {"name": "accept", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_maybe_accept"}},
            {"name": "email", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_type_email_address"}},
            {"name": "submit_email", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_submit_email_step"}},
            {"name": "next_state", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_wait_email_submit_next_state", "return_value": "otp"}},
            {"name": "otp_wait", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "wait_for_otp", "return_value": "123456"}},
            {"name": "clear_otp", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_clear_otp_inputs"}},
            {"name": "type_otp", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_type_otp"}},
            {"name": "click_continue", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_click_continue"}},
            {"name": "otp_outcome", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_wait_after_email_otp_submit", "return_value": "accepted"}},
            {"name": "phone", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "_do_phone_verification_if_present"}},
            {"name": "consent", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "_finish_consent_workspace", "return_value": callback_url}},
            {"name": "profile", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_complete_profile_page", "return_value": True}},
            {"name": "delay", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "human_delay"}},
            {"name": "fetch_session", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_fetch_chatgpt_session",
                "side_effect": RuntimeError("session 超时")}},
            {"name": "cookie_fallback", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_fetch_chatgpt_session_via_cookies", "return_value": None}},
            {"name": "save", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "save_account_data", "return_value": 42}},
            {"name": "source", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "resolve_email_source", "return_value": "icloud_hme"}},
            {"name": "auth", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._request_cpa_authorize_url", "return_value": {
                    "state": "state123",
                    "auth_url": "https://auth.openai.com/oauth/authorize?state=state123"}}},
            {"name": "code", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._extract_code", "return_value": "code123"}},
            {"name": "submit", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._submit_cpa_callback", "return_value": {
                    "status": "ok", "message": "submitted"}}},
            {"name": "save_cpa", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._save_cpa_local_record", "return_value": "/tmp/receipt.json"}},
            {"name": "verify", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._verify_cpa_auth_landed", "return_value": verify_result}},
        ])
        try:
            result = roxy_registration.run_roxy_registration_and_codex(
                "user@example.com", "Test User", "2000-01-01", proxy=None, otp_code=None, batch_dir=None,
            )
        finally:
            stack.close()

        mocks["verify"].assert_called_once_with("user@example.com")
        return result

    def test_one_shot_cpa_not_landed_marks_failed(self):
        result = self._run_one_shot(verify_result=False)
        codex = result.get("codex") or {}
        self.assertEqual(codex.get("status"), "failed")
        self.assertIs(codex.get("ok"), False)
        self.assertIn("未落盘可用 auth 文件", codex.get("message") or "")
        self.assertIs(result.get("success"), False)
        self.assertIn("Codex 未完成", result.get("error") or "")

    def test_one_shot_cpa_landed_marks_success(self):
        result = self._run_one_shot(verify_result=True)
        codex = result.get("codex") or {}
        self.assertEqual(codex.get("status"), "success")
        self.assertIs(codex.get("ok"), True)
        self.assertIs(result.get("success"), True)

    def test_one_shot_cookie_fallback_supplies_access_token(self):
        """页面 session 拿不到 AT 时，cookie 协议兜底成功 → access_token 写入结果。"""
        callback_url = "http://localhost:1455/auth/callback?code=code123&state=state123"
        client = Mock()
        client.open_profile.return_value = SimpleNamespace(profile_id="p1", raw={})
        driver = Mock()
        driver.current_url = "https://chatgpt.com/chat"

        stack, mocks = _patched([
            {"name": "client", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "RoxyBrowserClient", "return_value": client}},
            {"name": "build", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_build_driver", "return_value": driver}},
            {"name": "center", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_center_browser_window"}},
            {"name": "safe_get", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_safe_get"}},
            {"name": "warmup", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_page_warmup"}},
            {"name": "stop", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_check_manual_stop"}},
            {"name": "accept", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_maybe_accept"}},
            {"name": "email", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_type_email_address"}},
            {"name": "submit_email", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_submit_email_step"}},
            {"name": "next_state", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_wait_email_submit_next_state", "return_value": "otp"}},
            {"name": "otp_wait", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "wait_for_otp", "return_value": "123456"}},
            {"name": "clear_otp", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_clear_otp_inputs"}},
            {"name": "type_otp", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_type_otp"}},
            {"name": "click_continue", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_click_continue"}},
            {"name": "otp_outcome", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_wait_after_email_otp_submit", "return_value": "accepted"}},
            {"name": "phone", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "_do_phone_verification_if_present"}},
            {"name": "consent", "enter": patch.object, "kwargs": {
                "target": roxy_codex_oauth, "attribute": "_finish_consent_workspace", "return_value": callback_url}},
            {"name": "profile", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_complete_profile_page", "return_value": True}},
            {"name": "delay", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "human_delay"}},
            {"name": "fetch_session", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_fetch_chatgpt_session",
                "side_effect": RuntimeError("session 超时")}},
            {"name": "cookie_fallback", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "_fetch_chatgpt_session_via_cookies", "return_value": {
                    "accessToken": "sk-cookie-9", "user": {"id": "u9"}, "account": {}, "expires": "2026-01-01"}}},
            {"name": "save", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "save_account_data", "return_value": 43}},
            {"name": "source", "enter": patch.object, "kwargs": {
                "target": roxy_registration, "attribute": "resolve_email_source", "return_value": "icloud_hme"}},
            {"name": "auth", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._request_cpa_authorize_url", "return_value": {
                    "state": "state123",
                    "auth_url": "https://auth.openai.com/oauth/authorize?state=state123"}}},
            {"name": "code", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._extract_code", "return_value": "code123"}},
            {"name": "submit", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._submit_cpa_callback", "return_value": {
                    "status": "ok", "message": "submitted"}}},
            {"name": "save_cpa", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._save_cpa_local_record", "return_value": "/tmp/receipt.json"}},
            {"name": "verify", "enter": patch, "kwargs": {
                "target": "core.codex_oauth._verify_cpa_auth_landed", "return_value": True}},
        ])
        try:
            result = roxy_registration.run_roxy_registration_and_codex(
                "user@example.com", "Test User", "2000-01-01", proxy=None, otp_code=None, batch_dir=None,
            )
        finally:
            stack.close()

        self.assertEqual(result.get("access_token"), "sk-cookie-9")
        mocks["cookie_fallback"].assert_called_once()
        save_extra = mocks["save"].call_args.kwargs.get("extra") or {}
        self.assertEqual((save_extra.get("codex") or {}).get("status"), "success")


class FetchChatgptSessionWarningBannerTests(unittest.TestCase):
    """R4a：_fetch_chatgpt_session 对 WARNING_BANNER（无 accessToken）应主动刷新而不是白等。"""

    def test_warning_banner_triggers_jump_and_then_returns_session(self):
        """先返回 WARNING_BANNER，刷新后返回 accessToken → 返回 session，且跳转 /chat。"""
        driver = FakeDriver(
            current_url="https://chatgpt.com/chat",
            script_results=[
                {"ok": True, "data": {"WARNING_BANNER": "..."}},
                {"ok": True, "data": {"accessToken": "sk-after-refresh", "user": {"id": "u1"}}},
            ],
        )

        def fake_safe_get(driver, url, **kwargs):
            driver.get(url)

        with patch.object(roxy_registration, "_safe_get", side_effect=fake_safe_get), \
             patch.object(roxy_registration, "_switch_to_chatgpt_window_if_any", return_value=False), \
             patch.object(roxy_registration, "time") as mock_time:
            # call1=end, call2=auto_jump_end, call3/4=while 判断
            mock_time.time.side_effect = [0.0, 0.0, 0.0, 0.0]
            mock_time.sleep.return_value = None
            result = roxy_registration._fetch_chatgpt_session(
                driver, timeout=30, auto_jump_wait=1, banner_refresh_attempts=3, banner_refresh_delay=0.01,
            )

        self.assertEqual(result.get("accessToken"), "sk-after-refresh")
        self.assertIn("https://chatgpt.com/chat", driver.get_calls)

    def test_warning_banner_exhausts_refresh_then_raises(self):
        """WARNING_BANNER 持续存在，主动刷新达到上限后仍超时 → 抛 RuntimeError。"""
        driver = FakeDriver(
            current_url="https://chatgpt.com/chat",
            script_results=[
                {"ok": True, "data": {"WARNING_BANNER": "..."}},
                {"ok": True, "data": {"WARNING_BANNER": "..."}},
                {"ok": True, "data": {"WARNING_BANNER": "..."}},
                {"ok": True, "data": {"WARNING_BANNER": "..."}},
                {"ok": True, "data": {"WARNING_BANNER": "..."}},
            ],
        )

        def fake_safe_get(driver, url, **kwargs):
            driver.get(url)

        with patch.object(roxy_registration, "_safe_get", side_effect=fake_safe_get), \
             patch.object(roxy_registration, "_switch_to_chatgpt_window_if_any", return_value=False), \
             patch.object(roxy_registration, "time") as mock_time:
            # call1=end, call2=auto_jump_end；两轮刷新后第 3 次 while 判断直接超时退出
            mock_time.time.side_effect = [0.0, 0.0, 0.0, 0.0, 31.0]
            mock_time.sleep.return_value = None
            with self.assertRaises(RuntimeError):
                roxy_registration._fetch_chatgpt_session(
                    driver, timeout=30, auto_jump_wait=1, banner_refresh_attempts=2, banner_refresh_delay=0.01,
                )

        self.assertEqual(driver.get_calls.count("https://chatgpt.com/chat"), 2)


class CookieProtocolFallbackTests(unittest.TestCase):
    """R4b：one-shot 拿 AT 的 cookie 协议兜底。"""

    def test_cookie_fallback_returns_session(self):
        driver = FakeDriver()
        driver.get_cookies = Mock(return_value=[
            {"name": "__Secure-next-auth.session-token", "value": "tok", "domain": "chatgpt.com"},
            {"name": "unrelated", "value": "x", "domain": "example.com"},
        ])
        with patch("core.codex_agent.get_session_from_cookies", return_value={
            "accessToken": "sk-cookie",
            "user": {"id": "u9"},
        }) as mock_get:
            result = roxy_registration._fetch_chatgpt_session_via_cookies(driver)
        self.assertEqual(result.get("accessToken"), "sk-cookie")
        mock_get.assert_called_once()
        cookies = mock_get.call_args.args[0]
        self.assertIn("__Secure-next-auth.session-token", cookies)
        self.assertNotIn("unrelated", cookies)

    def test_cookie_fallback_no_cookies_returns_none(self):
        driver = FakeDriver()
        driver.get_cookies = Mock(return_value=[])
        with patch("core.codex_agent.get_session_from_cookies", side_effect=AssertionError("不应调用")):
            result = roxy_registration._fetch_chatgpt_session_via_cookies(driver)
        self.assertIsNone(result)

    def test_cookie_fallback_no_access_token_returns_none(self):
        driver = FakeDriver()
        driver.get_cookies = Mock(return_value=[
            {"name": "__Secure-next-auth.session-token", "value": "tok", "domain": "chatgpt.com"},
        ])
        with patch("core.codex_agent.get_session_from_cookies", return_value={"user": {}}):
            result = roxy_registration._fetch_chatgpt_session_via_cookies(driver)
        self.assertIsNone(result)

    def test_cookie_fallback_exception_returns_none(self):
        driver = FakeDriver()
        driver.get_cookies = Mock(return_value=[
            {"name": "__Secure-next-auth.session-token", "value": "tok", "domain": "chatgpt.com"},
        ])
        with patch("core.codex_agent.get_session_from_cookies", side_effect=RuntimeError("网络失败")):
            result = roxy_registration._fetch_chatgpt_session_via_cookies(driver)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
