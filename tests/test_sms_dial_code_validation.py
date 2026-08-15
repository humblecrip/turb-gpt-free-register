# -*- coding: utf-8 -*-
"""取号号码区号校验（防平台串号）单元测试。

覆盖：
    - 区号匹配/不匹配判定（33→+57 哥伦比亚 / 73→+55 巴西 / 187→+1 美国）
    - 未知国家码跳过校验（不误伤，打 DEBUG）
    - 映射表覆盖默认队列/配置中的国家码
    - acquire_number 取到串号 → 立即取消并抛 SmsNoNumbersError（切下一国家）
    - L / H / grizzly 三条取号链路都生效
"""
import unittest
from unittest.mock import Mock, patch

from config import codex as codex_config
from core import sms_provider


class DialCodeValidationTests(unittest.TestCase):
    def test_validate_phone_country_match(self):
        self.assertIsNone(sms_provider._validate_phone_country("573001234567", "33"))
        self.assertIsNone(sms_provider._validate_phone_country("5511987654321", "73"))
        self.assertIsNone(sms_provider._validate_phone_country("16195366483", "187"))
        self.assertIsNone(sms_provider._validate_phone_country("+55 41 99864 5771", "73"))

    def test_validate_phone_country_mismatch(self):
        # 请求 33（哥伦比亚 +57），平台串号返回巴西 +55 → 不匹配
        reason = sms_provider._validate_phone_country("5511987654321", "33")
        self.assertIsNotNone(reason)
        self.assertIn("33", reason)
        self.assertIn("57", reason)
        self.assertIn("+5511987654321", reason)

    def test_validate_phone_country_unknown_code_skips(self):
        # 未知国家码跳过校验，不误伤
        self.assertIsNone(sms_provider._validate_phone_country("1234567890", "999"))
        self.assertIsNone(sms_provider._validate_phone_country("", "73"))

    def test_dial_code_map_contains_queue_codes(self):
        # 映射表覆盖默认队列/配置中的国家码（33/73/187/57/6/54/76）
        for country in ("33", "73", "187", "57", "6", "54", "76"):
            self.assertIn(country, sms_provider._COUNTRY_DIAL_CODES)
        self.assertEqual(sms_provider._COUNTRY_DIAL_CODES["33"], "57")
        self.assertEqual(sms_provider._COUNTRY_DIAL_CODES["73"], "55")
        self.assertEqual(sms_provider._COUNTRY_DIAL_CODES["187"], "1")


class AcquireRejectsWrongCountryTests(unittest.TestCase):
    def test_l_acquire_rejects_wrong_country_phone(self):
        # L 取号返回串号（33 请求 → +55 巴西号）→ 释放号码并抛 SmsNoNumbersError
        http = _Http([
            {"item": {"id": "lid-1", "phone": "5511987654321"}},
            {"released": 1, "failed": []},
        ])
        with patch.object(codex_config, "SMS_PROVIDER", "l"), \
             patch.object(codex_config, "L_API_BASE", "http://localhost:8788"), \
             patch.object(codex_config, "L_ADMIN_AUTH_CODE", "adm"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_COUNTRY", "33"):
            with self.assertRaises(sms_provider.SmsNoNumbersError) as ctx:
                sms_provider.acquire_number(http=http, country="33")
        self.assertIn("区号不匹配", str(ctx.exception))
        # take-phone 之后立即 release 释放
        self.assertGreaterEqual(len(http.calls), 2)
        self.assertTrue(http.calls[1]["url"].endswith("/api/admin/l/release"))

    def test_l_acquire_accepts_matching_phone(self):
        http = _Http([{"item": {"id": "lid-2", "phone": "573001234567"}}])
        with patch.object(codex_config, "SMS_PROVIDER", "l"), \
             patch.object(codex_config, "L_API_BASE", "http://localhost:8788"), \
             patch.object(codex_config, "L_ADMIN_AUTH_CODE", "adm"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_COUNTRY", "33"):
            activation_id, phone = sms_provider.acquire_number(http=http, country="33")
        self.assertEqual(activation_id, "lid-2")
        self.assertEqual(phone, "573001234567")

    def test_grizzly_acquire_rejects_wrong_country_phone(self):
        # grizzly 取号返回串号（187 请求 → +55 巴西号）→ 取消并抛 SmsNoNumbersError
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
             patch.object(codex_config, "SMS_COUNTRY", "187"), \
             patch.object(sms_provider, "_request_grizzly",
                          return_value="ACCESS_NUMBER:act-g-1:5511987654321"), \
             patch.object(sms_provider, "cancel") as cancel, \
             patch.object(sms_provider, "_http", return_value=Mock()):
            with self.assertRaises(sms_provider.SmsNoNumbersError):
                sms_provider.acquire_number(country="187")
        cancel.assert_called_once()
        self.assertEqual(cancel.call_args.args[0], "act-g-1")

    def test_h_acquire_skips_validation_for_unknown_country_code(self):
        # H 使用字符串国家码（"us"）→ 无区号映射，跳过校验不误伤
        http = _Http([{"item": {"id": "hid-1", "phone": "2025550123"}, "reused": True, "duplicate": False}])
        with patch.object(codex_config, "SMS_PROVIDER", "h"), \
             patch.object(codex_config, "H_API_BASE", "http://localhost:8788"), \
             patch.object(codex_config, "H_ADMIN_AUTH_CODE", "adm"), \
             patch.object(codex_config, "SMS_SERVICE", "12345"), \
             patch.object(codex_config, "SMS_COUNTRY", "us"), \
             patch.object(codex_config, "H_PHONE_PREFIX", "1"):
            activation_id, phone = sms_provider.acquire_number(http=http, country="us")
        self.assertEqual(activation_id, "hid-1")
        self.assertEqual(phone, "12025550123")


class _Resp:
    status_code = 200
    text = "{}"

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, data=None):
        self.calls.append({"url": url, "headers": headers or {}, "data": data})
        return _Resp(self.responses.pop(0))

    def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
