# -*- coding: utf-8 -*-
"""一键扫描CPA失效号 + 邮箱deactivated判定 + 批量重上号的单元测试。

覆盖：
  - check_mailbox_has_deactivation：deactivated 主题匹配 / 非 OpenAI 不算 / 查询失败降级 False
  - cpa_scan_relogin_pipeline：扫→测活 live 跳过 / deactivated+有停用邮件跳过 /
    deactivated+无停用邮件进队列 / failed+无停用邮件进队列 / 不可重上跳过
"""
import unittest
from unittest.mock import patch

from core import cpa_reauth
from core.cpa_reauth import (
    DEACTIVATION_HINTS,
    _looks_like_deactivation_email,
    check_mailbox_has_deactivation,
    cpa_scan_relogin_pipeline,
)

DEAD_ACCOUNTS = [
    {"name": "codex-a@example.com-free.json", "email": "a@example.com", "status": "error",
     "disabled": False, "unavailable": False, "success": 0, "failed": 30,
     "reauthable": True, "dead_by": "meta"},
    {"name": "codex-b@example.com-free.json", "email": "b@example.com", "status": "disabled",
     "disabled": True, "unavailable": False, "success": 0, "failed": 0,
     "reauthable": True, "dead_by": "meta"},
    {"name": "codex-c@example.com-free.json", "email": "c@example.com", "status": "error",
     "disabled": False, "unavailable": False, "success": 0, "failed": 30,
     "reauthable": True, "dead_by": "meta"},
    {"name": "codex-d@example.com-free.json", "email": "d@example.com", "status": "error",
     "disabled": False, "unavailable": False, "success": 0, "failed": 30,
     "reauthable": False, "dead_by": "meta"},
]

REAUTH_RET = {
    "ok": True, "started": ["b@example.com"], "skipped": [],
    "batch_id": "20260815-000000", "results": [{"email": "b@example.com", "ok": True, "status": "success"}],
    "ok_count": 1, "failed_count": 0,
}


class DeactivationHintsTests(unittest.TestCase):
    def test_hints_include_core_keywords(self):
        for hint in ("access deactivated", "has been deactivated", "已停用", "账号已停用"):
            self.assertIn(hint, DEACTIVATION_HINTS)

    def test_looks_like_deactivation_matches_openai_subject(self):
        item = {"from": "no-reply@openai.com", "subject": "Your access has been deactivated",
                "text": "We've deactivated your account due to inactivity."}
        self.assertTrue(_looks_like_deactivation_email(item))

    def test_looks_like_deactivation_matches_chinese(self):
        item = {"from": "no-reply@openai.com", "subject": "您的 OpenAI 账号已停用",
                "text": "您的账号已停用，如有疑问请联系我们。"}
        self.assertTrue(_looks_like_deactivation_email(item))

    def test_non_openai_mail_not_counted(self):
        item = {"from": "news@example.com", "subject": "Your access has been deactivated",
                "text": "Your account has been deactivated."}
        self.assertFalse(_looks_like_deactivation_email(item))

    def test_normal_openai_otp_mail_not_counted(self):
        item = {"from": "no-reply@openai.com", "subject": "Your OpenAI code is 123456",
                "text": "Verification code 123456"}
        self.assertFalse(_looks_like_deactivation_email(item))


class CheckMailboxHasDeactivationTests(unittest.TestCase):
    def test_found_deactivation_returns_true(self):
        with patch("core.email_provider.resolve_email_source", return_value="outlook"), \
             patch("core.cpa_reauth._list_recent_emails", return_value=[
                 {"from": "no-reply@openai.com", "subject": "Your access has been deactivated", "text": "..."},
             ]):
            res = check_mailbox_has_deactivation("a@example.com")
        self.assertTrue(res["deactivated"])
        self.assertEqual(res["source"], "outlook")
        self.assertEqual(res["matched_subject"], "Your access has been deactivated")

    def test_no_deactivation_returns_false(self):
        with patch("core.email_provider.resolve_email_source", return_value="outlook"), \
             patch("core.cpa_reauth._list_recent_emails", return_value=[
                 {"from": "no-reply@openai.com", "subject": "Your OpenAI code is 123456", "text": "123456"},
             ]):
            res = check_mailbox_has_deactivation("a@example.com")
        self.assertFalse(res["deactivated"])
        self.assertIsNone(res["matched_subject"])

    def test_fetch_error_returns_false_with_error(self):
        with patch("core.email_provider.resolve_email_source", return_value="outlook"), \
             patch("core.cpa_reauth._list_recent_emails", side_effect=RuntimeError("boom")):
            res = check_mailbox_has_deactivation("a@example.com")
        self.assertFalse(res["deactivated"])
        self.assertIn("boom", res.get("error", ""))

    def test_resolve_error_returns_false_with_error(self):
        with patch("core.email_provider.resolve_email_source", side_effect=RuntimeError("no source")):
            res = check_mailbox_has_deactivation("a@example.com")
        self.assertFalse(res["deactivated"])
        self.assertIn("no source", res.get("error", ""))

    def test_empty_email_returns_false(self):
        res = check_mailbox_has_deactivation("")
        self.assertFalse(res["deactivated"])
        self.assertIn("email 为空", res.get("error", ""))


class ScanReloginPipelineTests(unittest.TestCase):
    def _run(self, liveness_side_effect, mailbox_side_effect, dead=DEAD_ACCOUNTS):
        with patch.object(cpa_reauth, "scan_cpa_dead_accounts", return_value=dead), \
             patch.object(cpa_reauth, "is_email_reauthable", return_value=True), \
             patch("core.account_liveness.check_account_liveness", side_effect=liveness_side_effect), \
             patch.object(cpa_reauth, "check_mailbox_has_deactivation", side_effect=mailbox_side_effect), \
             patch.object(cpa_reauth, "run_reauth_pipeline", return_value=REAUTH_RET) as mock_reauth:
            ret = cpa_reauth.cpa_scan_relogin_pipeline(max_total=50)
            return ret, mock_reauth

    def test_live_accounts_skipped_without_reauth(self):
        """测活判定 live → 跳过重上，不调 run_reauth_pipeline。"""
        ret, mock_reauth = self._run(
            liveness_side_effect=[{"ok": True, "status": "live"}] * 3,
            mailbox_side_effect=[{"deactivated": False}],
        )
        self.assertTrue(ret["ok"])
        self.assertEqual(ret["dead_total"], 4)
        self.assertEqual(ret["live"], 3)
        self.assertEqual(ret["to_reauth"], [])
        self.assertEqual(ret["deactivated_mailbox"], 0)
        mock_reauth.assert_not_called()
        self.assertEqual(len(ret["skipped"]), 4)  # 3 个 live + 1 个不可重上

    def test_deactivated_with_mailbox_mail_skipped(self):
        """deactivated + 邮箱有停用邮件 → 标记账号已废跳过；无邮件的进重上队列。"""
        liveness = [
            {"ok": False, "status": "deactivated", "error": "AT 过期"},   # a → 有停用邮件
            {"ok": False, "status": "deactivated", "error": "AT 过期"},   # b → 无停用邮件
            {"ok": False, "status": "failed", "error": "403"},            # c → 无停用邮件
        ]
        mailbox = [
            {"deactivated": True, "matched_subject": "Your access has been deactivated", "source": "outlook"},
            {"deactivated": False, "source": "outlook"},
            {"deactivated": False, "source": "outlook"},
        ]
        ret, mock_reauth = self._run(liveness, mailbox)
        self.assertEqual(ret["deactivated_mailbox"], 1)
        self.assertEqual(ret["to_reauth"], ["b@example.com", "c@example.com"])
        mock_reauth.assert_called_once()
        args, kwargs = mock_reauth.call_args
        self.assertEqual(args[0], ["b@example.com", "c@example.com"])
        self.assertTrue(kwargs["delete_first"])
        statuses = {r["email"]: r["status"] for r in ret["results"]}
        self.assertEqual(statuses["a@example.com"], "deactivated_mailbox")
        self.assertEqual(statuses["b@example.com"], "to_reauth")
        self.assertEqual(statuses["c@example.com"], "to_reauth")

    def test_skip_deactivated_mailbox_false_queues_all(self):
        """skip_deactivated_mailbox=False → 不查邮箱，deactivated/failed 全部进重上队列。"""
        liveness = [
            {"ok": False, "status": "deactivated"},
            {"ok": False, "status": "failed"},
        ]
        with patch.object(cpa_reauth, "scan_cpa_dead_accounts", return_value=DEAD_ACCOUNTS[:2]), \
             patch.object(cpa_reauth, "is_email_reauthable", return_value=True), \
             patch("core.account_liveness.check_account_liveness", side_effect=liveness), \
             patch.object(cpa_reauth, "check_mailbox_has_deactivation") as mock_mailbox, \
             patch.object(cpa_reauth, "run_reauth_pipeline", return_value=REAUTH_RET) as mock_reauth:
            ret = cpa_reauth.cpa_scan_relogin_pipeline(skip_deactivated_mailbox=False, max_total=50)
        self.assertEqual(ret["deactivated_mailbox"], 0)
        self.assertEqual(ret["to_reauth"], ["a@example.com", "b@example.com"])
        mock_mailbox.assert_not_called()
        mock_reauth.assert_called_once()

    def test_non_reauthable_skipped_before_liveness(self):
        """不可重上的号跳过，不测活、不进队列。"""
        only_dead = [dict(DEAD_ACCOUNTS[3])]  # d@example.com reauthable=False
        with patch.object(cpa_reauth, "scan_cpa_dead_accounts", return_value=only_dead), \
             patch.object(cpa_reauth, "is_email_reauthable", return_value=False), \
             patch("core.account_liveness.check_account_liveness") as mock_liveness, \
             patch.object(cpa_reauth, "check_mailbox_has_deactivation") as mock_mailbox, \
             patch.object(cpa_reauth, "run_reauth_pipeline") as mock_reauth:
            ret = cpa_reauth.cpa_scan_relogin_pipeline(max_total=50)
        self.assertEqual(ret["reauthable"], 0)
        self.assertEqual(ret["to_reauth"], [])
        self.assertEqual(len(ret["skipped"]), 1)
        mock_liveness.assert_not_called()
        mock_mailbox.assert_not_called()
        mock_reauth.assert_not_called()

    def test_scan_error_returns_ok_false(self):
        with patch.object(cpa_reauth, "scan_cpa_dead_accounts", side_effect=RuntimeError("CPA 挂了")):
            ret = cpa_reauth.cpa_scan_relogin_pipeline(max_total=50)
        self.assertFalse(ret["ok"])
        self.assertIn("CPA 挂了", ret.get("error", ""))

    def test_empty_scan_no_reauth(self):
        with patch.object(cpa_reauth, "scan_cpa_dead_accounts", return_value=[]), \
             patch.object(cpa_reauth, "run_reauth_pipeline") as mock_reauth:
            ret = cpa_reauth.cpa_scan_relogin_pipeline(max_total=50)
        self.assertTrue(ret["ok"])
        self.assertEqual(ret["dead_total"], 0)
        mock_reauth.assert_not_called()


if __name__ == "__main__":
    unittest.main()
