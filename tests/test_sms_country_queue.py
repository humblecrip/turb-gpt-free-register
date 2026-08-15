# -*- coding: utf-8 -*-
"""接码国家可维护优先级队列单元测试。

覆盖：
    - 队列解析：主+备选去重、空配置兜底、prefer 前置（参数 + 全局）
    - auto_price / auto_success 排序逻辑（mock HeroSMS 响应）
    - 0 库存 / 低成功率过滤；API 失败回落 manual；冷启动热门兜底
    - 本地成功率埋点记录/读取
    - 配置默认值与 config_editor 暴露
    - codex_oauth / roxy_codex_oauth 不再硬编码默认队列
"""
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config import codex as codex_config
from core import codex_oauth
from core import roxy_codex_oauth
from core import sms_provider
from webui import config_editor

_DEFAULT_STATS_FILE = Path(__file__).resolve().parent.parent / "data" / "sms_country_stats.json"

# _fetch_price_info 解析后的格式（供 auto 排序测试直接 mock）
_PRICES = {"54": 2.5, "73": 1.0, "76": 0.5, "33": 1.5}
_STATUS = {"54": 100, "73": 50, "33": 30}
_RATES = {"54": 0.9, "73": 0.8, "33": 0.6}


class CountryQueueManualTests(unittest.TestCase):
    def setUp(self):
        sms_provider.set_country_prefer(None)

    def test_manual_queue_from_config_dedup(self):
        with patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33,76,33"):
            queue = sms_provider.resolve_country_queue()
        self.assertEqual(queue, ["73", "33", "76"])

    def test_manual_queue_empty_falls_back_default(self):
        with patch.object(codex_config, "SMS_COUNTRY", ""), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", ""):
            queue = sms_provider.resolve_country_queue()
        self.assertEqual(queue, ["54", "76", "73", "33"])

    def test_prefer_param_prepends_and_dedup(self):
        with patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            queue = sms_provider.resolve_country_queue(prefer="33")
        self.assertEqual(queue, ["33", "73"])

    def test_global_prefer_prepends(self):
        with patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            sms_provider.set_country_prefer("76")
            try:
                queue = sms_provider.resolve_country_queue()
            finally:
                sms_provider.set_country_prefer(None)
        self.assertEqual(queue, ["76", "73", "33"])


class CountryQueueAutoTests(unittest.TestCase):
    def setUp(self):
        sms_provider.set_country_prefer(None)

    def _resolve(self, sort, rates=_RATES, top=None):
        top = [] if top is None else top
        with patch.object(sms_provider, "_fetch_price_info", return_value=(_PRICES, _STATUS)), \
             patch.object(sms_provider, "local_country_success_rates", return_value=rates), \
             patch.object(sms_provider, "_top_countries_from_api", return_value=top):
            return sms_provider.resolve_country_queue(sort=sort)

    def test_auto_price_sorts_price_asc_then_success_desc(self):
        # 76 库存为 0 被过滤；价格升序：73(1.0) → 33(1.5) → 54(2.5)
        self.assertEqual(self._resolve("auto_price"), ["73", "33", "54"])

    def test_auto_success_sorts_success_desc_then_price(self):
        # 成功率降序：54(0.9) → 73(0.8) → 33(0.6)
        self.assertEqual(self._resolve("auto_success"), ["54", "73", "33"])

    def test_fetch_price_info_parses_raw_responses(self):
        # 422 修复：getPrices/getNumbersStatus 按候选国家子集逐国家查询（带 country 参数）
        with patch.object(sms_provider, "_candidate_countries", return_value=["54", "73", "33"]), \
             patch.object(sms_provider, "_grizzly_json_action", side_effect=[
                 {"54": {"dr": {"cost": 2.5, "count": 100}}}, {"54": {"dr": 100}},
                 {"73": {"dr": {"cost": 1.0, "count": 50}}}, {"73": {"dr": 50}},
                 {"33": {"dr": {"cost": 1.5, "count": 30}}}, {"33": {"dr": 30}},
             ]):
            prices, status = sms_provider._fetch_price_info()
        # 只查询候选子集（54/73/33），不再返回全平台（76 不在候选内）
        self.assertEqual(prices, {"54": 2.5, "73": 1.0, "33": 1.5})
        self.assertEqual(status, {"54": 100, "73": 50, "33": 30})

    def test_grizzly_json_action_passes_country_param(self):
        # 422 根因修复：getPrices/getNumbersStatus 必须带数字 country 参数
        captured = []

        def fake_request(http, params):
            captured.append(dict(params))
            return "{}"

        with patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(sms_provider, "_request_grizzly", side_effect=fake_request), \
             patch.object(sms_provider, "_http", return_value=Mock()):
            sms_provider._grizzly_json_action("getPrices", country="54")
            sms_provider._grizzly_json_action("getNumbersStatus", country="73")
        self.assertEqual(captured[0]["action"], "getPrices")
        self.assertEqual(captured[0]["country"], "54")
        self.assertEqual(captured[0]["service"], "dr")
        self.assertEqual(captured[1]["action"], "getNumbersStatus")
        self.assertEqual(captured[1]["country"], "73")

    def test_grizzly_json_action_omits_country_when_empty(self):
        # getTopCountriesByService 等无需 country 的 action 不传 country 参数
        captured = []

        def fake_request(http, params):
            captured.append(dict(params))
            return "{}"

        with patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(sms_provider, "_request_grizzly", side_effect=fake_request), \
             patch.object(sms_provider, "_http", return_value=Mock()):
            sms_provider._grizzly_json_action("getTopCountriesByService", country="")
        self.assertEqual(captured[0]["action"], "getTopCountriesByService")
        self.assertNotIn("country", captured[0])

    def test_fetch_price_info_skips_single_country_failure(self):
        # 单国家查询失败 → 跳过该国并告警，不影响其他候选国家
        def fake_action(action, http=None, country=None):
            if country == "76":
                raise sms_provider.SmsProviderError("HTTP 422 ...")
            if action == "getPrices":
                return {country: {"dr": {"cost": 1.0, "count": 50}}}
            return {country: {"dr": 50}}

        with patch.object(sms_provider, "_candidate_countries", return_value=["76", "73"]), \
             patch.object(sms_provider, "_grizzly_json_action", side_effect=fake_action):
            prices, status = sms_provider._fetch_price_info()
        self.assertEqual(prices, {"73": 1.0})
        self.assertEqual(status, {"73": 50})

    def test_fetch_price_info_all_fail_raises(self):
        # 所有候选国家查询全部失败 → 抛错，由上层回落 manual 队列并告警
        with patch.object(sms_provider, "_candidate_countries", return_value=["76", "54"]), \
             patch.object(sms_provider, "_grizzly_json_action",
                          side_effect=sms_provider.SmsProviderError("HTTP 422 ...")):
            with self.assertRaises(sms_provider.SmsProviderError) as ctx:
                sms_provider._fetch_price_info()
        self.assertIn("全部失败", str(ctx.exception))

    def test_fetch_price_info_balance_raises_immediately(self):
        # 余额不足逐国家查询时立即透传（不跳过该国继续刷接口）
        with patch.object(sms_provider, "_candidate_countries", return_value=["76", "73"]), \
             patch.object(sms_provider, "_grizzly_json_action",
                          side_effect=sms_provider.SmsNoBalanceError("余额不足")):
            with self.assertRaises(sms_provider.SmsNoBalanceError):
                sms_provider._fetch_price_info()

    def test_candidate_countries_builds_subset(self):
        # 候选 = manual 主队列 + prefer + 平台热门（去重；热门仅作补充）
        with patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"), \
             patch.object(sms_provider, "_top_countries_from_api", return_value=["54", "76", "187", "33"]), \
             patch.object(sms_provider, "_task_country_prefer", return_value="187"):
            candidates = sms_provider._candidate_countries()
        self.assertEqual(candidates, ["73", "33", "187", "54", "76"])

    def test_candidate_countries_top_failure_still_returns_base(self):
        # 平台热门拉取失败 → 候选仍包含主队列，不中断
        with patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"), \
             patch.object(sms_provider, "_top_countries_from_api",
                          side_effect=sms_provider.SmsProviderError("boom")):
            candidates = sms_provider._candidate_countries()
        self.assertEqual(candidates, ["73", "33"])

    def test_top_countries_parses_flat_format(self):
        with patch.object(sms_provider, "_grizzly_json_action", return_value={"54": 100, "73": 50, "33": 30}):
            self.assertEqual(sms_provider._top_countries_from_api(), ["54", "73", "33"])

    def test_top_countries_parses_nested_format(self):
        # sms-online/GrizzlySMS 风格：外层是数字序号，国家码在 "country" 字段
        nested = {
            "0": {"country": 33, "count": 43575, "price": 15.0},
            "1": {"country": 54, "count": 100, "price": 20.0},
        }
        with patch.object(sms_provider, "_grizzly_json_action", return_value=nested):
            self.assertEqual(sms_provider._top_countries_from_api(), ["33", "54"])

    def test_top_countries_ignores_zero_and_malformed(self):
        nested = {
            "0": {"country": 33, "count": 0},
            "1": {"country": 54, "count": 100},
            "2": {"country": 73, "count": "oops"},
            "3": {"country": 76, "count": None},
        }
        with patch.object(sms_provider, "_grizzly_json_action", return_value=nested):
            self.assertEqual(sms_provider._top_countries_from_api(), ["54"])

    def test_auto_filters_low_success_countries(self):
        rates = {"54": 0.9, "73": 0.8, "33": 0.2}
        self.assertEqual(self._resolve("auto_price", rates=rates), ["73", "54"])

    def test_auto_cold_start_uses_top_countries_as_neutral(self):
        # 无本地历史：热门国家给中性成功率 0.5（>0.3 过滤线）；76 库存 0 仍被过滤
        self.assertEqual(self._resolve("auto_price", rates={}, top=["54", "73", "33"]), ["73", "33", "54"])

    def test_auto_api_failure_falls_back_manual(self):
        with patch.object(sms_provider, "_fetch_price_info", side_effect=sms_provider.SmsProviderError("boom")), \
             patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            queue = sms_provider.resolve_country_queue(sort="auto_price")
        self.assertEqual(queue, ["73", "33"])

    def test_non_grizzly_provider_auto_falls_back_manual(self):
        with patch.object(codex_config, "SMS_PROVIDER", "h"), \
             patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"), \
             patch.object(sms_provider, "_fetch_price_info", side_effect=AssertionError("不应调 API")):
            queue = sms_provider.resolve_country_queue(sort="auto_price")
        self.assertEqual(queue, ["73", "33"])

    def test_sort_reads_config_when_param_missing(self):
        with patch.object(codex_config, "SMS_COUNTRY_SORT", "auto_price"), \
             patch.object(sms_provider, "_fetch_price_info", return_value=(_PRICES, _STATUS)), \
             patch.object(sms_provider, "local_country_success_rates", return_value=_RATES), \
             patch.object(sms_provider, "_top_countries_from_api", return_value=[]):
            queue = sms_provider.resolve_country_queue()
        self.assertEqual(queue, ["73", "33", "54"])


class CountryQueueStatsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._stats_file = Path(self._tmpdir.name) / "sms_country_stats.json"
        self._orig_stats_file = sms_provider._SMS_STATS_FILE
        sms_provider._SMS_STATS_FILE = self._stats_file
        sms_provider.set_country_prefer(None)

    def tearDown(self):
        sms_provider._SMS_STATS_FILE = self._orig_stats_file
        sms_provider.set_country_prefer(None)
        self._tmpdir.cleanup()

    def test_record_and_read_stats(self):
        sms_provider.record_sms_result("73", True)
        sms_provider.record_sms_result("73", True)
        sms_provider.record_sms_result("33", False)
        rates = sms_provider.local_country_success_rates()
        self.assertEqual(rates, {"73": 1.0, "33": 0.0})
        data = json.loads(self._stats_file.read_text(encoding="utf-8"))
        self.assertEqual(data["73"], {"success": 2, "failed": 0})
        self.assertEqual(data["33"], {"success": 0, "failed": 1})

    def test_record_ignores_empty_country(self):
        sms_provider.record_sms_result("", True)
        sms_provider.record_sms_result(None, False)
        self.assertFalse(self._stats_file.exists())

    def test_local_rates_without_stats_file(self):
        self.assertEqual(sms_provider.local_country_success_rates(), {})


class CountryQueueCallSiteTests(unittest.TestCase):
    def test_codex_oauth_uses_shared_round_loop(self):
        src = inspect.getsource(codex_oauth._do_phone_verification)
        self.assertIn("run_country_queue_rounds", src)
        self.assertNotIn('"54", "76", "73", "33"', src)

    def test_roxy_codex_oauth_uses_shared_round_loop(self):
        src = inspect.getsource(roxy_codex_oauth._do_phone_verification_if_present)
        self.assertIn("run_country_queue_rounds", src)
        self.assertNotIn('"54", "76", "73", "33"', src)


class CountryQueueConfigTests(unittest.TestCase):
    def test_config_defaults(self):
        self.assertEqual(codex_config.SMS_COUNTRY_SORT, "manual")
        self.assertTrue(codex_config.SMS_FALLBACK_COUNTRIES)

    def test_webui_exposes_new_fields(self):
        fields = {f["key"]: f for f in config_editor.EDITABLE_FIELDS}
        self.assertIn("SMS_FALLBACK_COUNTRIES", fields)
        self.assertIn("SMS_COUNTRY_SORT", fields)
        self.assertEqual(fields["SMS_FALLBACK_COUNTRIES"]["group"], "接码平台")
        self.assertEqual(fields["SMS_COUNTRY_SORT"]["group"], "接码平台")


if __name__ == "__main__":
    unittest.main()
