# -*- coding: utf-8 -*-
"""单测：轻量 token 探测查活（check_account_liveness_light）与 mode 降级逻辑。

覆盖：
- light: 200→live / 401→deactivated / token_expired→deactivated / 403→failed / 无 token→failed 提示重登录
- auto: light live/deactivated → 不降级 full；light failed → 降级 full
- light/full 模式互不串扰；非法 mode 报错
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import account_liveness


def _account(access_token="at-test-123"):
    return {"email": "user@example.com", "access_token": access_token}


class LivenessLightBase(unittest.TestCase):
    """把查活日志重定向到临时目录，避免污染仓库 注册日志/。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        log_dir = Path(self._tmp.name)
        patcher = mock.patch.object(
            account_liveness,
            "log_path",
            side_effect=lambda email: log_dir / f"live-check-{email}.log",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class CheckAccountLivenessLightTests(LivenessLightBase):
    """轻量 token 探测三态映射。"""

    def _run(self, account=None, probe=None, plan_side_effect=None):
        with mock.patch.object(account_liveness.db, "get_account_by_email", return_value=account) as db_mock, \
             mock.patch.object(account_liveness, "check_account_plan",
                               side_effect=plan_side_effect if plan_side_effect is not None else (lambda *a, **k: probe)) as plan_mock:
            result = account_liveness.check_account_liveness_light(
                email="user@example.com", proxy=None, clear_log=True
            )
        return result, db_mock, plan_mock

    def test_http_200_is_live(self):
        result, db_mock, plan_mock = self._run(
            account=_account(),
            probe={"ok": True, "http_status": 200, "current_plan_type": "chatgptplusplan"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "live")
        self.assertEqual(result["access_token"], "at-test-123")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["plan_type"], "chatgptplusplan")
        plan_mock.assert_called_once()

    def test_http_401_is_deactivated(self):
        result, _, _ = self._run(
            account=_account(),
            probe={"ok": False, "http_status": 401, "error": "AT已过期/失效，请手动查活刷新"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "deactivated")
        self.assertIn("AT已过期", result["error"])

    def test_token_expired_claim_is_deactivated(self):
        """JWT 本地解析已过期（无 http_status）也算 deactivated。"""
        result, _, _ = self._run(
            account=_account(),
            probe={"ok": False, "http_status": None, "token_expired": True,
                   "error": "AT已过期/失效，请手动查活刷新"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "deactivated")

    def test_http_403_is_failed(self):
        result, _, _ = self._run(
            account=_account(),
            probe={"ok": False, "http_status": 403, "error": "HTTP 403"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["http_status"], 403)

    def test_network_error_is_failed(self):
        result, _, _ = self._run(
            account=_account(),
            probe={"ok": False, "http_status": None, "error": "ProxyError: connection timeout"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")

    def test_probe_raises_is_failed(self):
        result, _, _ = self._run(
            account=_account(),
            plan_side_effect=RuntimeError("proxy timeout"),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("proxy timeout", result["error"])

    def test_no_token_suggests_relogin_without_probe(self):
        result, _, plan_mock = self._run(account=_account(access_token=""))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("需重新登录", result["error"])
        plan_mock.assert_not_called()

    def test_account_missing_suggests_relogin(self):
        result, _, plan_mock = self._run(account=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("需重新登录", result["error"])
        plan_mock.assert_not_called()

    def test_db_lookup_error_is_failed(self):
        with mock.patch.object(account_liveness.db, "get_account_by_email",
                               side_effect=OSError("json corrupt")) as db_mock, \
             mock.patch.object(account_liveness, "check_account_plan") as plan_mock:
            result = account_liveness.check_account_liveness_light("user@example.com")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("读取账号库失败", result["error"])
        plan_mock.assert_not_called()
        db_mock.assert_called_once()


class CheckAccountLivenessModeTests(LivenessLightBase):
    """check_account_liveness 的 mode 分发与 auto 降级。"""

    def _light_probe(self, status, http_status=None):
        if status == "live":
            return {"ok": True, "status": "live", "http_status": 200, "access_token": "at"}
        if status == "deactivated":
            return {"ok": False, "status": "deactivated", "http_status": 401, "error": "AT已过期"}
        return {"ok": False, "status": "failed", "http_status": http_status, "error": "HTTP 403"}

    def test_auto_light_live_returns_without_full(self):
        with mock.patch.object(account_liveness, "_probe_liveness_light",
                               return_value=self._light_probe("live")), \
             mock.patch.object(account_liveness, "_check_liveness_full",
                               return_value={"ok": True, "status": "live", "checked_at": "x"}) as full_mock:
            result = account_liveness.check_account_liveness("user@example.com", mode="auto")
        self.assertEqual(result["status"], "live")
        full_mock.assert_not_called()

    def test_auto_light_deactivated_returns_without_full(self):
        with mock.patch.object(account_liveness, "_probe_liveness_light",
                               return_value=self._light_probe("deactivated")), \
             mock.patch.object(account_liveness, "_check_liveness_full",
                               return_value={"ok": True, "status": "live", "checked_at": "x"}) as full_mock:
            result = account_liveness.check_account_liveness("user@example.com", mode="auto")
        self.assertEqual(result["status"], "deactivated")
        full_mock.assert_not_called()

    def test_auto_light_failed_degrades_to_full(self):
        with mock.patch.object(account_liveness, "_probe_liveness_light",
                               return_value=self._light_probe("failed", http_status=403)), \
             mock.patch.object(account_liveness, "_check_liveness_full",
                               return_value={"ok": True, "status": "live", "checked_at": "x", "access_token": "new-at"}) as full_mock:
            result = account_liveness.check_account_liveness("user@example.com", mode="auto")
        self.assertEqual(result["status"], "live")
        self.assertEqual(result["access_token"], "new-at")
        full_mock.assert_called_once()

    def test_auto_light_failed_full_confirms_failed(self):
        with mock.patch.object(account_liveness, "_probe_liveness_light",
                               return_value=self._light_probe("failed", http_status=403)), \
             mock.patch.object(account_liveness, "_check_liveness_full",
                               return_value={"ok": False, "status": "failed", "checked_at": "x",
                                            "error": "Exception: OTP timeout"}) as full_mock:
            result = account_liveness.check_account_liveness("user@example.com", mode="auto")
        self.assertEqual(result["status"], "failed")
        full_mock.assert_called_once()

    def test_mode_light_skips_full(self):
        with mock.patch.object(account_liveness, "_probe_liveness_light",
                               return_value=self._light_probe("failed", http_status=403)), \
             mock.patch.object(account_liveness, "_check_liveness_full",
                               return_value={"ok": True, "status": "live"}) as full_mock:
            result = account_liveness.check_account_liveness("user@example.com", mode="light")
        self.assertEqual(result["status"], "failed")
        full_mock.assert_not_called()

    def test_mode_full_skips_light(self):
        with mock.patch.object(account_liveness, "_probe_liveness_light",
                               return_value=self._light_probe("failed")) as light_mock, \
             mock.patch.object(account_liveness, "_check_liveness_full",
                               return_value={"ok": True, "status": "live", "checked_at": "x"}):
            result = account_liveness.check_account_liveness("user@example.com", mode="full")
        self.assertEqual(result["status"], "live")
        light_mock.assert_not_called()

    def test_auto_with_real_db_no_token_degrades_to_full(self):
        """auto 且账号库无 token：light 判 failed（提示重登录），降级 full。"""
        with mock.patch.object(account_liveness.db, "get_account_by_email",
                               return_value=_account(access_token="")), \
             mock.patch.object(account_liveness, "check_account_plan") as plan_mock, \
             mock.patch.object(account_liveness, "_check_liveness_full",
                               return_value={"ok": True, "status": "live", "checked_at": "x", "access_token": "new-at"}) as full_mock:
            result = account_liveness.check_account_liveness("user@example.com", mode="auto")
        self.assertEqual(result["status"], "live")
        plan_mock.assert_not_called()
        full_mock.assert_called_once()

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            account_liveness.check_account_liveness("user@example.com", mode="bogus")


if __name__ == "__main__":
    unittest.main()
