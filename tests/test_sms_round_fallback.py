# -*- coding: utf-8 -*-
"""接码国家队列兜底优化单元测试。

覆盖：
    - 兜底国家池：manual 主队列自动扩展 auto_price 兜底池（去重）；auto 排序/非 grizzly 不扩展；
      API 失败回落主队列；余额不足立即透传
    - 多轮重试：轮间等待、主队列→兜底池顺序、SmsNoNumbersError 不耗 attempt/轮次、
      SMS_MAX_RETRIES 每轮生效
    - 错误分层：平台无号 / 平台接口异常（连续阈值或主导）/ 余额不足立即停
    - 不埋点：NO_NUMBERS 不记入本地成功率埋点
"""
import unittest
from unittest.mock import Mock, patch

from config import codex as codex_config
from core import codex_oauth
from core import sms_provider
from webui import config_editor


class FallbackPoolTests(unittest.TestCase):
    def setUp(self):
        sms_provider.set_country_prefer(None)
        sms_provider.clear_task_sms_override()

    def test_manual_round_queue_expands_to_auto_price_pool(self):
        # 主队列 73,33 无号 → 自动扩展为有库存国家按价格升序兜底：73(1.0) 33(1.5) 54(2.5)
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
             patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"), \
             patch.object(sms_provider, "_fetch_price_info", return_value=(
                 {"54": 2.5, "73": 1.0, "33": 1.5},
                 {"54": 100, "73": 50, "33": 30},
             )), \
             patch.object(sms_provider, "local_country_success_rates", return_value={"54": 0.9, "73": 0.8, "33": 0.6}), \
             patch.object(sms_provider, "_top_countries_from_api", return_value=[]):
            queue = sms_provider._round_country_queue(sort="manual")
        self.assertEqual(queue, ["73", "33", "54"])

    def test_fallback_pool_empty_for_auto_sort(self):
        # auto 排序主队列本身已是全平台排序结果，不再扩展兜底池（不应再调 API）
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
             patch.object(codex_config, "SMS_COUNTRY_SORT", "auto_price"), \
             patch.object(sms_provider, "_fetch_price_info", side_effect=AssertionError("不应调 API")):
            pool = sms_provider._fallback_country_pool(["73", "33"], sort="auto_price")
        self.assertEqual(pool, [])

    def test_fallback_pool_empty_for_non_grizzly(self):
        # l/h 平台无价格数据，兜底回落主队列本身（靠多轮重试）
        with patch.object(codex_config, "SMS_PROVIDER", "h"), \
             patch.object(codex_config, "SMS_COUNTRY_SORT", "manual"), \
             patch.object(sms_provider, "_fetch_price_info", side_effect=AssertionError("不应调 API")):
            pool = sms_provider._fallback_country_pool(["73", "33"], sort="manual")
        self.assertEqual(pool, [])

    def test_fallback_pool_excludes_base_countries(self):
        # 兜底池排除主队列已试国家，不重复试
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
             patch.object(codex_config, "SMS_COUNTRY_SORT", "manual"), \
             patch.object(sms_provider, "_auto_sorted_country_queue", return_value=["73", "54", "33", "76"]):
            pool = sms_provider._fallback_country_pool(["73", "33"], sort="manual")
        self.assertEqual(pool, ["54", "76"])

    def test_fallback_pool_api_failure_returns_empty(self):
        # 兜底池拉取失败 → 回落主队列多轮重试
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
             patch.object(codex_config, "SMS_COUNTRY_SORT", "manual"), \
             patch.object(sms_provider, "_auto_sorted_country_queue", side_effect=sms_provider.SmsProviderError("boom")):
            pool = sms_provider._fallback_country_pool(["73", "33"], sort="manual")
        self.assertEqual(pool, [])

    def test_fallback_pool_balance_raises_immediately(self):
        # 兜底池拉取遇余额不足 → 立即透传（不回落主队列）
        with patch.object(codex_config, "SMS_PROVIDER", "grizzly"), \
             patch.object(codex_config, "SMS_COUNTRY_SORT", "manual"), \
             patch.object(sms_provider, "_auto_sorted_country_queue", side_effect=sms_provider.SmsNoBalanceError("余额不足")):
            with self.assertRaises(sms_provider.SmsNoBalanceError):
                sms_provider._fallback_country_pool(["73", "33"], sort="manual")


class CountryQueueRoundsTests(unittest.TestCase):
    def setUp(self):
        sms_provider.set_country_prefer(None)
        sms_provider.clear_task_sms_override()

    def test_success_on_first_country(self):
        calls = []

        def try_country(country, round_no, attempt):
            calls.append((country, round_no, attempt))
            return True

        with patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]), \
             patch.object(sms_provider.time, "sleep") as sleep:
            result = sms_provider.run_country_queue_rounds(try_country, round_retries=3, round_wait=30)
        self.assertTrue(result)
        self.assertEqual(calls, [("73", 1, 1)])
        sleep.assert_not_called()

    def test_no_numbers_switches_country_without_consuming_attempt(self):
        calls = []

        def try_country(country, round_no, attempt):
            calls.append((country, attempt))
            if country in ("73", "33"):
                raise sms_provider.SmsNoNumbersError(f"{country} 无号")
            return True

        with patch.object(sms_provider, "_round_country_queue", return_value=["73", "33", "54"]), \
             patch.object(sms_provider.time, "sleep") as sleep:
            result = sms_provider.run_country_queue_rounds(try_country, round_retries=2, round_wait=30)
        self.assertTrue(result)
        # 73/33 无号不耗 attempt：第三次仍是 attempt=1，在 54 成功，无需轮间等待
        self.assertEqual(calls, [("73", 1), ("33", 1), ("54", 1)])
        sleep.assert_not_called()

    def test_fallback_pool_country_tried_after_main_queue(self):
        calls = []

        def try_country(country, round_no, attempt):
            calls.append(country)
            if country in ("73", "33"):
                raise sms_provider.SmsNoNumbersError(f"{country} 无号")
            return True

        # 主队列 73,33 无号 → 兜底池 54 有号，同一轮内成功
        with patch.object(sms_provider, "_round_country_queue", return_value=["73", "33", "54"]):
            result = sms_provider.run_country_queue_rounds(try_country, round_retries=1, round_wait=0)
        self.assertTrue(result)
        self.assertEqual(calls, ["73", "33", "54"])

    def test_platform_error_retries_same_country(self):
        calls = []

        def try_country(country, round_no, attempt):
            calls.append(country)
            if len(calls) < 3:
                raise sms_provider.SmsProviderError("平台错误")
            return True

        with patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]):
            result = sms_provider.run_country_queue_rounds(try_country, max_retries=5, round_retries=1, round_wait=0)
        self.assertTrue(result)
        # 平台异常不切国家：3 次都尝试 73
        self.assertEqual(calls, ["73", "73", "73"])

    def test_all_no_numbers_raises_no_numbers_error(self):
        def try_country(country, round_no, attempt):
            raise sms_provider.SmsNoNumbersError(f"{country} 无号")

        with patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]), \
             patch.object(sms_provider.time, "sleep"):
            with self.assertRaises(sms_provider.SmsQueueExhaustedError) as ctx:
                sms_provider.run_country_queue_rounds(try_country, round_retries=3, round_wait=0)
        self.assertIn("接码平台当前无可用号码", str(ctx.exception))
        self.assertIn("已重试 3 轮", str(ctx.exception))

    def test_platform_failure_consecutive_threshold_classified(self):
        def try_country(country, round_no, attempt):
            raise sms_provider.SmsProviderError("网络抖动")

        with patch.object(sms_provider, "_round_country_queue", return_value=["73"]):
            with self.assertRaises(sms_provider.SmsQueueExhaustedError) as ctx:
                sms_provider.run_country_queue_rounds(try_country, max_retries=5, round_retries=1, round_wait=0)
        self.assertIn("接码平台接口异常", str(ctx.exception))

    def test_platform_errors_dominant_classified(self):
        # 平台错误多于无号（连续未达 5 次阈值）→ 判平台接口异常
        seq = iter(["err", "err", "err", "no"])

        def try_country(country, round_no, attempt):
            kind = next(seq)
            if kind == "no":
                raise sms_provider.SmsNoNumbersError(f"{country} 无号")
            raise sms_provider.SmsProviderError("平台错误")

        with patch.object(sms_provider, "_round_country_queue", return_value=["73"]):
            with self.assertRaises(sms_provider.SmsQueueExhaustedError) as ctx:
                sms_provider.run_country_queue_rounds(try_country, max_retries=3, round_retries=2, round_wait=0)
        self.assertIn("接码平台接口异常", str(ctx.exception))

    def test_balance_raises_immediately(self):
        calls = []

        def try_country(country, round_no, attempt):
            calls.append(country)
            raise sms_provider.SmsNoBalanceError("余额不足")

        with patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]):
            with self.assertRaises(sms_provider.SmsNoBalanceError):
                sms_provider.run_country_queue_rounds(try_country, round_retries=3, round_wait=0)
        # 只尝试了第一个国家即停止，不进入下一轮
        self.assertEqual(calls, ["73"])

    def test_round_wait_sleeps_between_rounds(self):
        def try_country(country, round_no, attempt):
            raise sms_provider.SmsNoNumbersError(f"{country} 无号")

        with patch.object(sms_provider, "_round_country_queue", return_value=["73"]), \
             patch.object(sms_provider.time, "sleep") as sleep:
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                sms_provider.run_country_queue_rounds(try_country, round_retries=3, round_wait=30)
        # 3 轮之间只等 2 次
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_any_call(30)

    def test_max_retries_applied_per_round(self):
        calls = []

        def try_country(country, round_no, attempt):
            calls.append((round_no, attempt))
            raise sms_provider.SmsProviderError("平台错误")

        with patch.object(sms_provider, "_round_country_queue", return_value=["73"]), \
             patch.object(sms_provider.time, "sleep"):
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                sms_provider.run_country_queue_rounds(try_country, max_retries=2, round_retries=2, round_wait=0)
        # 每轮换号次数独立：每轮 2 次，共 2 轮
        self.assertEqual(calls, [(1, 1), (1, 2), (2, 1), (2, 2)])

    def test_exhausted_error_is_runtime_error(self):
        # SmsQueueExhaustedError 是 RuntimeError 子类，错误文本可直接透传 WebUI
        def try_country(country, round_no, attempt):
            raise sms_provider.SmsNoNumbersError(f"{country} 无号")

        with patch.object(sms_provider, "_round_country_queue", return_value=["73"]):
            with self.assertRaises(RuntimeError) as ctx:
                sms_provider.run_country_queue_rounds(try_country, round_retries=1, round_wait=0)
        self.assertIn("接码平台当前无可用号码", str(ctx.exception))


class NoNumbersTelemetryTests(unittest.TestCase):
    def test_codex_oauth_no_numbers_not_recorded(self):
        # NO_NUMBERS 不记入本地成功率埋点；多轮重试后判平台无号
        calls = []

        def fake_acquire(http, country=None):
            calls.append(country)
            raise sms_provider.SmsNoNumbersError(f"{country} 无号")

        with patch.object(sms_provider, "acquire_number", side_effect=fake_acquire), \
             patch.object(sms_provider, "record_sms_result") as record, \
             patch.object(sms_provider, "_http", return_value=Mock()), \
             patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]), \
             patch.object(sms_provider.time, "sleep"):
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                codex_oauth._do_phone_verification(session=None)
        # 主队列两国无号，自动扩展兜底后仍然无号 → 判平台无号；NO_NUMBERS 不埋点
        self.assertEqual(calls, ["73", "33", "73", "33", "73", "33"])
        record.assert_not_called()


class CodexCallSiteCleanupTests(unittest.TestCase):
    """codex_oauth 调用点：取号成功后失败必须释放号码 + 记录埋点；余额立即停。"""

    def _patch_flow(self, **extra):
        p = patch.object(sms_provider, "_http", return_value=Mock())
        p.start()
        self.addCleanup(p.stop)
        for name, val in {
            "SMS_MAX_RETRIES": 1,
            "SMS_ROUND_RETRIES": 1,
            "SMS_ROUND_WAIT": 0,
            **extra,
        }.items():
            patcher = patch.object(codex_config, name, val)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_codex_post_acquire_failure_cancels_and_records(self):
        # 取号成功后 set_status 失败（平台接口异常）→ 释放号码 + 记录失败，不泄漏号、不丢埋点
        ok_resp = Mock(status_code=200)
        ok_resp.json.return_value = {}
        with patch.object(sms_provider, "acquire_number", return_value=("act1", "16195366483")), \
             patch.object(sms_provider, "set_status", side_effect=sms_provider.SmsProviderError("setStatus HTTP 500")), \
             patch.object(codex_oauth, "_post_json", return_value=ok_resp), \
             patch.object(sms_provider, "cancel") as cancel, \
             patch.object(sms_provider, "record_sms_result") as record, \
             patch.object(sms_provider, "_round_country_queue", return_value=["73"]), \
             patch.object(sms_provider.time, "sleep"):
            self._patch_flow()
            with self.assertRaises(sms_provider.SmsQueueExhaustedError):
                codex_oauth._do_phone_verification(session=None)
        cancel.assert_called_once()
        record.assert_called_with("73", False)

    def test_codex_balance_after_acquire_raises_immediately(self):
        # 取号成功后 set_status 报余额不足 → 立即透传，不换号、不进下一轮
        ok_resp = Mock(status_code=200)
        ok_resp.json.return_value = {}
        calls = []

        def fake_set_status(activation_id, status, http=None):
            calls.append(activation_id)
            raise sms_provider.SmsNoBalanceError("接码平台余额不足（NO_BALANCE），请充值")

        with patch.object(sms_provider, "acquire_number", return_value=("act1", "16195366483")), \
             patch.object(sms_provider, "set_status", side_effect=fake_set_status), \
             patch.object(codex_oauth, "_post_json", return_value=ok_resp), \
             patch.object(sms_provider, "cancel") as cancel, \
             patch.object(sms_provider, "record_sms_result") as record, \
             patch.object(sms_provider, "_round_country_queue", return_value=["73", "33"]), \
             patch.object(sms_provider.time, "sleep"):
            self._patch_flow()
            with self.assertRaises(sms_provider.SmsNoBalanceError):
                codex_oauth._do_phone_verification(session=None)
        self.assertEqual(calls, ["act1"])
        cancel.assert_not_called()
        record.assert_not_called()


class CountryRoundConfigTests(unittest.TestCase):
    def test_round_config_defaults(self):
        self.assertEqual(codex_config.SMS_ROUND_RETRIES, 3)
        self.assertEqual(codex_config.SMS_ROUND_WAIT, 30)

    def test_webui_exposes_round_fields(self):
        fields = {f["key"]: f for f in config_editor.EDITABLE_FIELDS}
        self.assertIn("SMS_ROUND_RETRIES", fields)
        self.assertIn("SMS_ROUND_WAIT", fields)
        self.assertEqual(fields["SMS_ROUND_RETRIES"]["group"], "接码平台")
        self.assertEqual(fields["SMS_ROUND_WAIT"]["group"], "接码平台")


if __name__ == "__main__":
    unittest.main()
