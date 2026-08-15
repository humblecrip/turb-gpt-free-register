# -*- coding: utf-8 -*-
"""已用号码黑名单（phone already used）单元测试。

覆盖：
    - 黑名单读写：规范化（去 +/空格）+ 国家码 + 时间；精确匹配
    - 清理：30 天过期 + 每国家 500 条封顶（保存时自动清理）
    - codex 侧：already used → 写黑名单 + 同国家换号（不切国家）；
      取号后命中黑名单立即 cancel 不提交 OpenAI；whatsapp/invalid 仍切国家
    - roxy 侧：_classify_phone_page_failure 识别 already used；
      同国家换号 + 写黑名单
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config import codex as codex_config
from core import codex_oauth
from core import roxy_codex_oauth
from core import sms_provider


class _FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        raise ValueError("not json")


class UsedPhoneStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._used_file = Path(self._tmpdir.name) / "used_phones.json"
        self._orig_file = sms_provider._USED_PHONES_FILE
        sms_provider._USED_PHONES_FILE = self._used_file

    def tearDown(self):
        sms_provider._USED_PHONES_FILE = self._orig_file
        self._tmpdir.cleanup()

    def test_mark_phone_used_writes_normalized_with_country_and_time(self):
        sms_provider.mark_phone_used("73", "+55 11 96195366483")
        data = json.loads(self._used_file.read_text(encoding="utf-8"))
        self.assertEqual(data["73"][0]["phone"], "551196195366483")
        self.assertIsInstance(data["73"][0]["ts"], (int, float))
        self.assertTrue(sms_provider.is_phone_blacklisted("73", "+551196195366483"))

    def test_exact_match_only(self):
        sms_provider.mark_phone_used("73", "551196195366483")
        self.assertTrue(sms_provider.is_phone_blacklisted("73", "551196195366483"))
        self.assertFalse(sms_provider.is_phone_blacklisted("73", "551196195366484"))
        self.assertFalse(sms_provider.is_phone_blacklisted("33", "551196195366483"))

    def test_mark_ignores_empty_country_or_phone(self):
        sms_provider.mark_phone_used("", "551196195366483")
        sms_provider.mark_phone_used("73", "")
        sms_provider.mark_phone_used("73", "+  ")
        self.assertFalse(self._used_file.exists())

    def test_dedupe_on_remark(self):
        sms_provider.mark_phone_used("73", "111111")
        sms_provider.mark_phone_used("73", "111111")
        data = json.loads(self._used_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data["73"]), 1)

    def test_cleanup_expired_on_save(self):
        old_ts = sms_provider._USED_PHONES_MAX_AGE + 3600
        self._used_file.write_text(json.dumps({
            "73": [
                {"phone": "111111", "ts": time.time() - old_ts},
                {"phone": "222222", "ts": time.time()},
            ],
        }), encoding="utf-8")
        sms_provider.mark_phone_used("73", "333333")
        data = json.loads(self._used_file.read_text(encoding="utf-8"))
        phones = {e["phone"] for e in data["73"]}
        self.assertNotIn("111111", phones)
        self.assertIn("222222", phones)
        self.assertIn("333333", phones)

    def test_cleanup_caps_per_country(self):
        now = time.time()
        entries = [{"phone": f"phone{i:03d}", "ts": now - (600 - i)} for i in range(600)]
        self._used_file.write_text(json.dumps({"73": entries}), encoding="utf-8")
        sms_provider.mark_phone_used("73", "999999")
        data = json.loads(self._used_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data["73"]), 500)
        self.assertIn("999999", {e["phone"] for e in data["73"]})


class PhoneFailureReasonTests(unittest.TestCase):
    def test_codex_reason_used_too_many_is_phone_used(self):
        # used too many 不应被 send_limited 的 too many 抢先吞掉
        self.assertEqual(
            codex_oauth._phone_failure_reason("This phone number has been used too many times", 400),
            "phone_used_or_max",
        )

    def test_codex_reason_already_used(self):
        self.assertEqual(
            codex_oauth._phone_failure_reason("This phone number has already been used", 400),
            "phone_used_or_max",
        )
        self.assertEqual(
            codex_oauth._phone_failure_reason("该手机号已被使用", 400),
            "phone_used_or_max",
        )

    def test_roxy_classify_detects_already_used(self):
        self.assertEqual(
            roxy_codex_oauth._classify_phone_page_failure({"bodyText": "This phone number has already been used"}),
            "phone_used_or_max",
        )
        self.assertEqual(
            roxy_codex_oauth._classify_phone_page_failure({"bodyText": "This phone number has been used too many times"}),
            "phone_used_or_max",
        )
        self.assertEqual(
            roxy_codex_oauth._classify_phone_page_failure({"bodyText": "该手机号已被使用"}),
            "phone_used_or_max",
        )

    def test_roxy_classify_plain_too_many_still_send_limited(self):
        self.assertEqual(
            roxy_codex_oauth._classify_phone_page_failure({"bodyText": "Too many requests, try again later"}),
            "send_limited",
        )


class CodexUsedPhoneFlowTests(unittest.TestCase):
    def _patch_flow(self, **extra):
        p = patch.object(sms_provider, "_http", return_value=Mock())
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(codex_oauth, "_sleep_before_phone_retry")
        p.start()
        self.addCleanup(p.stop)
        for name, val in {
            "SMS_MAX_RETRIES": 2,
            "SMS_ROUND_RETRIES": 1,
            "SMS_ROUND_WAIT": 0,
            **extra,
        }.items():
            patcher = patch.object(codex_config, name, val)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_blacklist_hit_after_acquire_cancels_without_submitting(self):
        # 取号后命中黑名单 → 立即 cancel 换号，不提交 OpenAI，同国家重试
        with patch.object(sms_provider, "acquire_number", side_effect=[
            ("act1", "16195366483"),
            ("act2", "16195366484"),
        ]), \
             patch.object(sms_provider, "is_phone_blacklisted", return_value=True), \
             patch.object(sms_provider, "cancel") as cancel, \
             patch.object(sms_provider, "record_sms_result") as record, \
             patch.object(sms_provider, "mark_phone_used") as mark, \
             patch.object(codex_oauth, "_post_json", side_effect=AssertionError("不应提交 OpenAI")), \
             patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]):
            self._patch_flow()
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                codex_oauth._do_phone_verification(session=None)
        self.assertEqual(cancel.call_count, 2)
        record.assert_not_called()
        mark.assert_not_called()

    def test_blacklist_hit_acquires_are_all_same_country(self):
        # 黑名单命中不切国家：两次尝试都落在 73
        calls = []

        def fake_acquire(http, country=None):
            calls.append(country)
            return ("act", "16195366483")

        with patch.object(sms_provider, "acquire_number", side_effect=fake_acquire), \
             patch.object(sms_provider, "is_phone_blacklisted", return_value=True), \
             patch.object(sms_provider, "cancel"), \
             patch.object(sms_provider, "record_sms_result"), \
             patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]):
            self._patch_flow()
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                codex_oauth._do_phone_verification(session=None)
        self.assertEqual(calls, ["73", "73"])

    def test_already_used_at_send_marks_blacklist_and_retries_same_country(self):
        # add-phone/send 返回 already used → 写黑名单 + 同国家换号（不切国家）
        calls = []

        def fake_acquire(http, country=None):
            calls.append(country)
            return ("act", "16195366483")

        resp = _FakeResp(400, "This phone number has been used too many times")
        with patch.object(sms_provider, "acquire_number", side_effect=fake_acquire), \
             patch.object(sms_provider, "is_phone_blacklisted", return_value=False), \
             patch.object(sms_provider, "cancel") as cancel, \
             patch.object(sms_provider, "record_sms_result") as record, \
             patch.object(sms_provider, "mark_phone_used") as mark, \
             patch.object(codex_oauth, "_post_json", return_value=resp), \
             patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]):
            self._patch_flow()
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                codex_oauth._do_phone_verification(session=None)
        self.assertEqual(calls, ["73", "73"])
        self.assertEqual(mark.call_count, 2)
        mark.assert_called_with("73", "16195366483")
        cancel.assert_called()
        record.assert_called_with("73", False)

    def test_whatsapp_still_switches_country(self):
        # WhatsApp 通道保持现状：切下一国家，不写黑名单
        calls = []

        def fake_acquire(http, country=None):
            calls.append(country)
            return ("act", "16195366483")

        resp = _FakeResp(400, "WhatsApp channel not supported")
        with patch.object(sms_provider, "acquire_number", side_effect=fake_acquire), \
             patch.object(sms_provider, "is_phone_blacklisted", return_value=False), \
             patch.object(sms_provider, "cancel"), \
             patch.object(sms_provider, "record_sms_result"), \
             patch.object(sms_provider, "mark_phone_used") as mark, \
             patch.object(codex_oauth, "_post_json", return_value=resp), \
             patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]):
            self._patch_flow()
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                codex_oauth._do_phone_verification(session=None)
        self.assertEqual(calls, ["73", "33"])
        mark.assert_not_called()

    def test_invalid_phone_still_switches_country(self):
        calls = []

        def fake_acquire(http, country=None):
            calls.append(country)
            return ("act", "16195366483")

        resp = _FakeResp(400, "This phone number is not valid")
        with patch.object(sms_provider, "acquire_number", side_effect=fake_acquire), \
             patch.object(sms_provider, "is_phone_blacklisted", return_value=False), \
             patch.object(sms_provider, "cancel"), \
             patch.object(sms_provider, "record_sms_result"), \
             patch.object(sms_provider, "mark_phone_used") as mark, \
             patch.object(codex_oauth, "_post_json", return_value=resp), \
             patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]):
            self._patch_flow()
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                codex_oauth._do_phone_verification(session=None)
        self.assertEqual(calls, ["73", "33"])
        mark.assert_not_called()


class RoxyUsedPhoneFlowTests(unittest.TestCase):
    def _patch_driver_flow(self, wait_error, queue):
        driver = Mock()
        p = patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        for name, patch_target in [
            ("_is_phone_code_page", lambda: patch.object(roxy_codex_oauth, "_is_phone_code_page", return_value=True)),
            ("_ensure_add_phone_input", lambda: patch.object(roxy_codex_oauth, "_ensure_add_phone_input")),
            ("_set_phone_value", lambda: patch.object(roxy_codex_oauth, "_set_phone_value", return_value={"e164": "+16195366483"})),
            ("_blur_active_input_and_wait", lambda: patch.object(roxy_codex_oauth, "_blur_active_input_and_wait")),
            ("_verify_add_phone_value_before_submit", lambda: patch.object(roxy_codex_oauth, "_verify_add_phone_value_before_submit", return_value={"visibleValue": "+16195366483"})),
            ("_select_sms_channel_or_raise", lambda: patch.object(roxy_codex_oauth, "_select_sms_channel_or_raise")),
            ("_click_add_phone_continue_button", lambda: patch.object(roxy_codex_oauth, "_click_add_phone_continue_button", return_value={"ok": True})),
            ("_wait_page_settle_after_submit", lambda: patch.object(roxy_codex_oauth, "_wait_page_settle_after_submit")),
            ("_wait_after_phone_send", lambda: patch.object(roxy_codex_oauth, "_wait_after_phone_send", side_effect=RuntimeError(wait_error))),
            ("_refresh_add_phone_for_retry", lambda: patch.object(roxy_codex_oauth, "_refresh_add_phone_for_retry")),
            ("_sleep_before_phone_retry", lambda: patch.object(roxy_codex_oauth, "_sleep_before_phone_retry")),
        ]:
            patcher = patch_target()
            patcher.start()
            self.addCleanup(patcher.stop)
        p = patch.object(sms_provider, "_http", return_value=Mock())
        p.start()
        self.addCleanup(p.stop)
        for name, val in {
            "SMS_MAX_RETRIES": 2,
            "SMS_ROUND_RETRIES": 1,
            "SMS_ROUND_WAIT": 0,
        }.items():
            patcher = patch.object(codex_config, name, val)
            patcher.start()
            self.addCleanup(patcher.stop)
        return driver

    def test_already_used_page_marks_blacklist_and_retries_same_country(self):
        calls = []

        def fake_acquire(http, country=None):
            calls.append(country)
            return ("act", "16195366483")

        driver = self._patch_driver_flow(
            "phone_used_or_max: This phone number has been used too many times",
            queue=["73", "33"],
        )
        with patch.object(sms_provider, "acquire_number", side_effect=fake_acquire), \
             patch.object(sms_provider, "is_phone_blacklisted", return_value=False), \
             patch.object(sms_provider, "cancel"), \
             patch.object(sms_provider, "record_sms_result") as record, \
             patch.object(sms_provider, "mark_phone_used") as mark, \
             patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]):
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                roxy_codex_oauth._do_phone_verification_if_present(driver)
        # already used 不切国家：两次尝试都落在 73；写黑名单
        self.assertEqual(calls, ["73", "73"])
        self.assertEqual(mark.call_count, 2)
        mark.assert_called_with("73", "16195366483")
        record.assert_called_with("73", False)

    def test_roxy_whatsapp_still_switches_country(self):
        calls = []

        def fake_acquire(http, country=None):
            calls.append(country)
            return ("act", "16195366483")

        driver = self._patch_driver_flow(
            "whatsapp_channel: Please use WhatsApp",
            queue=["73", "33"],
        )
        with patch.object(sms_provider, "acquire_number", side_effect=fake_acquire), \
             patch.object(sms_provider, "is_phone_blacklisted", return_value=False), \
             patch.object(sms_provider, "cancel"), \
             patch.object(sms_provider, "record_sms_result"), \
             patch.object(sms_provider, "mark_phone_used") as mark, \
             patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]):
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                roxy_codex_oauth._do_phone_verification_if_present(driver)
        self.assertEqual(calls, ["73", "33"])
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
