# -*- coding: utf-8 -*-
"""CPA callback 提交后落盘校验（_verify_cpa_auth_landed）的单元测试。

背景：CPA 流程提交 callback 返回 {"status":"ok"} ≠ CPA 侧真实落盘。
_verify_cpa_auth_landed 通过轮询 CPA auth-files + 下载校验 access_token
来区分真/假 success。本测试 mock find/download 两个 CPA 交互函数。
"""
import unittest
from unittest.mock import patch

from core import codex_oauth


class CpaCallbackVerifyTests(unittest.TestCase):
    def setUp(self):
        # 不让测试真睡；delay 参数也可单独覆盖
        patcher = patch("core.codex_oauth.time.sleep")
        self.mock_sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_attempt_hit_returns_true(self):
        """首次轮询即找到可用文件（status=ok + access_token 非空）→ True。"""
        with patch.object(
            codex_oauth,
            "find_cpa_codex_auth_file",
            return_value={"name": "codex-user@example.com-free.json", "status": "ok"},
        ) as mock_find, patch.object(
            codex_oauth,
            "download_cpa_codex_auth_text",
            return_value=('{"access_token": "sk-test-123", "refresh_token": "rt-1"}', "codex-user@example.com-free.json", {}),
        ) as mock_download:
            result = codex_oauth._verify_cpa_auth_landed("user@example.com", max_attempts=3, delay=0.1)

        self.assertTrue(result)
        mock_find.assert_called_once()
        mock_download.assert_called_once_with(cpa_name="codex-user@example.com-free.json")

    def test_status_error_polls_all_attempts_returns_false(self):
        """找到文件但 status=error → 视为未落盘，全轮询后返回 False。"""
        with patch.object(
            codex_oauth,
            "find_cpa_codex_auth_file",
            return_value={"name": "codex-user@example.com-free.json", "status": "error"},
        ) as mock_find:
            result = codex_oauth._verify_cpa_auth_landed("user@example.com", max_attempts=3, delay=0.1)

        self.assertFalse(result)
        self.assertEqual(mock_find.call_count, 3)
        self.assertEqual(self.mock_sleep.call_count, 2)  # 只在 attempt < max_attempts 时睡

    def test_status_unavailable_and_disabled_returns_false(self):
        """status=unavailable/disabled 同样视为不可用 → False。"""
        for bad_status in ("unavailable", "disabled"):
            with patch.object(
                codex_oauth,
                "find_cpa_codex_auth_file",
                return_value={"name": "codex-user@example.com-free.json", "status": bad_status},
            ) as mock_find:
                result = codex_oauth._verify_cpa_auth_landed("user@example.com", max_attempts=2, delay=0.1)
            self.assertFalse(result, f"status={bad_status} 应视为未落盘")
            self.assertEqual(mock_find.call_count, 2)

    def test_download_without_access_token_returns_false(self):
        """下载内容无 access_token（如仅有 refresh_token）→ 未落盘 False。"""
        with patch.object(
            codex_oauth,
            "find_cpa_codex_auth_file",
            return_value={"name": "codex-user@example.com-free.json", "status": "ok"},
        ) as mock_find, patch.object(
            codex_oauth,
            "download_cpa_codex_auth_text",
            return_value=('{"refresh_token": "rt-1"}', "codex-user@example.com-free.json", {}),
        ) as mock_download:
            result = codex_oauth._verify_cpa_auth_landed("user@example.com", max_attempts=2, delay=0.1)

        self.assertFalse(result)
        self.assertEqual(mock_find.call_count, 2)
        self.assertEqual(mock_download.call_count, 2)

    def test_find_exception_degrades_to_false_within_max_attempts(self):
        """find 持续抛异常 → 降级 False，且调用次数不超过 max_attempts。"""
        with patch.object(
            codex_oauth,
            "find_cpa_codex_auth_file",
            side_effect=RuntimeError("CPA 管理接口不可达"),
        ) as mock_find:
            result = codex_oauth._verify_cpa_auth_landed("user@example.com", max_attempts=3, delay=0.1)

        self.assertFalse(result)
        self.assertEqual(mock_find.call_count, 3)

    def test_download_exception_degrades_and_continues_polling(self):
        """找到文件但下载/解析异常 → 本轮视为未命中，继续轮询到耗尽。"""
        with patch.object(
            codex_oauth,
            "find_cpa_codex_auth_file",
            return_value={"name": "codex-user@example.com-free.json", "status": "ok"},
        ) as mock_find, patch.object(
            codex_oauth,
            "download_cpa_codex_auth_text",
            side_effect=RuntimeError("下载内容不是有效 JSON"),
        ) as mock_download:
            result = codex_oauth._verify_cpa_auth_landed("user@example.com", max_attempts=3, delay=0.1)

        self.assertFalse(result)
        self.assertEqual(mock_find.call_count, 3)
        self.assertEqual(mock_download.call_count, 3)

    def test_late_landing_returns_true(self):
        """前几轮未落盘、最后轮才落盘 → 返回 True（轮询窗口内命中）。"""
        find_values = [
            None,  # 第一轮还没落盘
            {"name": "codex-user@example.com-free.json", "status": "error"},  # 第二轮已落但 error
            {"name": "codex-user@example.com-free.json", "status": "ok"},  # 第三轮可用
        ]
        with patch.object(
            codex_oauth,
            "find_cpa_codex_auth_file",
            side_effect=find_values,
        ) as mock_find, patch.object(
            codex_oauth,
            "download_cpa_codex_auth_text",
            return_value=('{"access_token": "sk-late-9"}', "codex-user@example.com-free.json", {}),
        ):
            result = codex_oauth._verify_cpa_auth_landed("user@example.com", max_attempts=3, delay=0.1)

        self.assertTrue(result)
        self.assertEqual(mock_find.call_count, 3)


class CpaCallbackVerifyConfigTests(unittest.TestCase):
    """config/codex.py 新增的校验参数应存在且类型正确。"""

    def test_verify_config_attributes_exist(self):
        from config import codex as codex_cfg

        self.assertTrue(hasattr(codex_cfg, "CPA_CALLBACK_VERIFY_RETRIES"))
        self.assertTrue(hasattr(codex_cfg, "CPA_CALLBACK_VERIFY_DELAY"))
        self.assertIsInstance(codex_cfg.CPA_CALLBACK_VERIFY_RETRIES, int)
        self.assertIsInstance(codex_cfg.CPA_CALLBACK_VERIFY_DELAY, float)


if __name__ == "__main__":
    unittest.main()
