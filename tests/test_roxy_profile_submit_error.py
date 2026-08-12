# -*- coding: utf-8 -*-
"""资料页提交错误检测 + GPTMail 域名池回写时机的单元测试。"""
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from core import roxy_registration


class DetectProfileSubmitErrorTests(unittest.TestCase):
    def _state(self, **overrides):
        state = {
            "url": "https://chatgpt.com/auth/about-you",
            "title": "About you",
            "inputs": [],
            "buttons": [],
            "errors": [],
            "text": "Continue",
        }
        state.update(overrides)
        return state

    def test_returns_none_on_normal_page(self):
        driver = Mock()
        with patch.object(roxy_registration, "_email_otp_page_state", return_value=self._state()):
            self.assertIsNone(roxy_registration._detect_profile_submit_error(driver))

    def test_detects_unsupported_email_japanese(self):
        driver = Mock()
        with patch.object(roxy_registration, "_email_otp_page_state", return_value=self._state(text="入力されたメールアドレスはサポートされていません")):
            err = roxy_registration._detect_profile_submit_error(driver)
        self.assertIsNotNone(err)
        self.assertIn("unsupported_email", err)

    def test_detects_unsupported_email_english(self):
        driver = Mock()
        with patch.object(roxy_registration, "_email_otp_page_state", return_value=self._state(text="The email address you entered is not supported")):
            err = roxy_registration._detect_profile_submit_error(driver)
        self.assertIsNotNone(err)
        self.assertIn("unsupported_email", err)

    def test_detects_unsupported_email_error_code_text(self):
        driver = Mock()
        with patch.object(roxy_registration, "_email_otp_page_state", return_value=self._state(text="不明なエラーが発生しました error_code: unsupported_email")):
            err = roxy_registration._detect_profile_submit_error(driver)
        self.assertIsNotNone(err)
        self.assertIn("unsupported_email", err)

    def test_detects_visible_field_errors(self):
        driver = Mock()
        with patch.object(roxy_registration, "_email_otp_page_state", return_value=self._state(errors=["入力されたメールアドレスはサポートされていません"])):
            err = roxy_registration._detect_profile_submit_error(driver)
        self.assertIsNotNone(err)

    def test_returns_none_when_snapshot_has_js_error(self):
        driver = Mock()
        with patch.object(roxy_registration, "_email_otp_page_state", return_value={"url": "", "error": "TimeoutException: msg"}):
            self.assertIsNone(roxy_registration._detect_profile_submit_error(driver))

    def test_returns_none_when_snapshot_raises(self):
        driver = Mock()
        with patch.object(roxy_registration, "_email_otp_page_state", side_effect=RuntimeError("boom")):
            self.assertIsNone(roxy_registration._detect_profile_submit_error(driver))


class CompleteProfilePageErrorTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]

    def _fake_time(self):
        return self.clock[0]

    def _advance(self, _):
        self.clock[0] += 1.0

    def _deps(self, submit_error=None):
        return [
            patch.object(roxy_registration, "_has_access_token", return_value=False),
            patch.object(roxy_registration, "_page_snapshot", return_value={"url": "https://chatgpt.com/auth/about-you", "text": "About you"}),
            patch.object(roxy_registration, "_is_profile_like", return_value=True),
            patch.object(roxy_registration, "_select_or_type", return_value=True),
            patch.object(roxy_registration, "_fill_birthday_or_age", return_value="birthday"),
            patch.object(roxy_registration, "_accept_profile_consents", return_value=0),
            patch.object(roxy_registration, "_click_if_enabled_submit", return_value=True),
            patch.object(roxy_registration, "_detect_profile_submit_error", return_value=submit_error),
            patch.object(roxy_registration, "human_delay"),
            patch.object(roxy_registration.time, "time", side_effect=self._fake_time),
            patch.object(roxy_registration.time, "sleep", side_effect=self._advance),
        ]

    def test_raises_when_submit_rejected_with_unsupported_email(self):
        driver = Mock()
        with ExitStack() as stack:
            for p in self._deps(submit_error="unsupported_email（OpenAI 拒绝该邮箱域名）"):
                stack.enter_context(p)
            with self.assertRaisesRegex(RuntimeError, "资料页提交被拒.*unsupported_email"):
                roxy_registration._complete_profile_page(driver, "Test User", "1995-01-01", timeout=5)

    def test_returns_true_when_submit_accepted(self):
        driver = Mock()
        with ExitStack() as stack:
            for p in self._deps(submit_error=None):
                stack.enter_context(p)
            self.assertTrue(roxy_registration._complete_profile_page(driver, "Test User", "1995-01-01", timeout=5))


class RoxyRegistrationWritebackTests(unittest.TestCase):
    def setUp(self):
        self._stack = ExitStack()

    def tearDown(self):
        self._stack.close()

    def _base_mocks(self, resolve_source=None):
        opened = Mock()
        opened.profile_id = "profile-1"
        opened.raw = {"data": {"driver": ""}}
        opened.debugger_address = ""
        opened.webdriver_url = ""
        s = self._stack
        s.enter_context(patch.object(roxy_registration.RoxyBrowserClient, "open_profile", return_value=opened))
        s.enter_context(patch.object(roxy_registration.RoxyBrowserClient, "cleanup_profile"))
        s.enter_context(patch.object(roxy_registration, "_build_driver", return_value=Mock()))
        s.enter_context(patch.object(roxy_registration, "_center_browser_window"))
        s.enter_context(patch.object(roxy_registration, "_safe_get"))
        s.enter_context(patch.object(roxy_registration, "_page_warmup"))
        s.enter_context(patch.object(roxy_registration, "_maybe_accept"))
        s.enter_context(patch.object(roxy_registration, "_check_manual_stop"))
        s.enter_context(patch.object(roxy_registration, "_submit_email_and_wait_next", return_value="otp"))
        s.enter_context(patch.object(roxy_registration, "_fill_password_page_if_present", return_value=None))
        s.enter_context(patch.object(roxy_registration, "wait_for_otp", return_value="123456"))
        s.enter_context(patch.object(roxy_registration, "_clear_otp_inputs"))
        s.enter_context(patch.object(roxy_registration, "_type_otp"))
        s.enter_context(patch.object(roxy_registration, "_click_continue"))
        s.enter_context(patch.object(roxy_registration, "_wait_after_email_otp_submit", return_value="accepted"))
        s.enter_context(patch.object(roxy_registration, "_complete_profile_page", return_value=True))
        s.enter_context(patch.object(roxy_registration, "save_account_data", return_value="account-1"))
        s.enter_context(patch.object(roxy_registration, "human_delay"))
        s.enter_context(patch.object(roxy_registration, "_click_resend_email_otp"))
        s.enter_context(patch.object(roxy_registration.time, "sleep"))
        s.enter_context(patch.object(roxy_registration._twofa_cfg, "ENABLE_2FA", False))
        s.enter_context(patch("config.codex.ENABLE_CODEX_AUTO", False))
        s.enter_context(patch("core.email_provider.release_email"))
        if resolve_source is not None:
            s.enter_context(patch.object(roxy_registration, "resolve_email_source", return_value=resolve_source))

    def _session(self, access_token="at-1"):
        return {"accessToken": access_token, "user": {"name": "t"}, "account": {}, "expires": 0}

    @patch("core.gptmail_client.record_register_result")
    def test_success_writeback_plus_one_for_gptmail(self, record_result):
        self._base_mocks(resolve_source="gptmail")
        with patch.object(roxy_registration, "_fetch_chatgpt_session", return_value=self._session()):
            result = roxy_registration.run_roxy_registration("user@gptmail.test", "Test User", "1995-01-01", proxy="")
        self.assertTrue(result["success"])
        record_result.assert_called_once_with("user@gptmail.test", True)

    @patch("core.gptmail_client.record_register_result")
    def test_success_no_writeback_for_non_gptmail(self, record_result):
        self._base_mocks(resolve_source="outlook")
        with patch.object(roxy_registration, "_fetch_chatgpt_session", return_value=self._session()):
            result = roxy_registration.run_roxy_registration("user@outlook.test", "Test User", "1995-01-01", proxy="")
        self.assertTrue(result["success"])
        record_result.assert_not_called()

    @patch("core.gptmail_client.record_register_result")
    def test_session_failure_writeback_minus_one_for_gptmail(self, record_result):
        self._base_mocks(resolve_source="gptmail")
        with patch.object(roxy_registration, "_fetch_chatgpt_session", side_effect=RuntimeError("等待 /api/auth/session accessToken 超时")):
            result = roxy_registration.run_roxy_registration("user@gptmail.test", "Test User", "1995-01-01", proxy="")
        self.assertFalse(result["success"])
        record_result.assert_called_once_with("user@gptmail.test", False)

    @patch("core.gptmail_client.record_register_result")
    def test_failure_writeback_fires_before_release_clears_gptmail_context(self, record_result):
        """真实运行顺序回归：release_email 会清空 gptmail 上下文缓存，
        失败回写必须先于 release 判定 gptmail，否则 -1 静默失效。"""
        from core.gptmail_client import _CONTEXT_CACHE, _cache_key

        email = "user@gptmail.test"
        _CONTEXT_CACHE[_cache_key(email)] = Mock()
        self.addCleanup(_CONTEXT_CACHE.pop, _cache_key(email), None)

        # 不 mock resolve_email_source，走真实上下文判定（resolve_source=None）
        self._base_mocks()

        # 模拟真实 release_email：回收 gptmail 邮箱时清掉上下文缓存
        def _fake_release(e, status="available", note=None):
            _CONTEXT_CACHE.pop(_cache_key(e), None)

        self._stack.enter_context(patch("core.email_provider.release_email", side_effect=_fake_release))

        with patch.object(roxy_registration, "_fetch_chatgpt_session", side_effect=RuntimeError("session 超时")):
            result = roxy_registration.run_roxy_registration(email, "Test User", "1995-01-01", proxy="")

        self.assertFalse(result["success"])
        record_result.assert_called_once_with(email, False)

    @patch("core.gptmail_client.record_register_result")
    def test_profile_rejection_writeback_minus_one_for_gptmail(self, record_result):
        self._base_mocks(resolve_source="gptmail")
        with patch.object(roxy_registration, "_complete_profile_page", side_effect=RuntimeError("资料页提交被拒: unsupported_email（OpenAI 拒绝该邮箱域名）")):
            result = roxy_registration.run_roxy_registration("user@gptmail.test", "Test User", "1995-01-01", proxy="")
        self.assertFalse(result["success"])
        record_result.assert_called_once_with("user@gptmail.test", False)

    @patch("core.gptmail_client.record_register_result")
    def test_otp_timeout_does_not_double_writeback_minus_one(self, record_result):
        self._base_mocks(resolve_source="gptmail")
        with patch.object(roxy_registration, "wait_for_otp", side_effect=RuntimeError("等待 GPTMail 验证码超时: user@gptmail.test")):
            result = roxy_registration.run_roxy_registration("user@gptmail.test", "Test User", "1995-01-01", proxy="")
        self.assertFalse(result["success"])
        record_result.assert_not_called()

    @patch("core.gptmail_client.record_register_result")
    def test_failure_writeback_does_not_break_non_gptmail(self, record_result):
        self._base_mocks(resolve_source="outlook")
        with patch.object(roxy_registration, "_fetch_chatgpt_session", side_effect=RuntimeError("session 超时")):
            result = roxy_registration.run_roxy_registration("user@outlook.test", "Test User", "1995-01-01", proxy="")
        self.assertFalse(result["success"])
        record_result.assert_not_called()


if __name__ == "__main__":
    unittest.main()
