# -*- coding: utf-8 -*-
"""「一键扫+重上」实时进度回调的单元测试。

覆盖：
    - _on_scan_relogin_entry：scan 阶段计数（scanned/live/reauthable/deactivated/to_reauth）
    - _on_scan_relogin_entry：reauth 阶段计数（started/ok_count/failed_count）
    - _on_scan_relogin_entry：logs 追加 + current 更新 + 日志结构
    - _on_scan_relogin_entry：logs 截断上限（_CPA_SCAN_RELOGIN_LOG_LIMIT）
    - _on_scan_relogin_entry：batch_id 不匹配时忽略
    - cpa_scan_relogin_pipeline：callback 每号触发，且透传给 run_reauth_pipeline
    - run_reauth_pipeline：每完成一个号回调
"""
import unittest
from unittest.mock import patch

from core import cpa_reauth
from webui.app import (
    _CPA_SCAN_RELOGIN_LOG_LIMIT,
    _CPA_SCAN_RELOGIN_LOCK,
    _CPA_SCAN_RELOGIN_STATE,
    _on_scan_relogin_entry,
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
]

REAUTH_RET = {
    "ok": True, "started": ["a@example.com"], "skipped": [],
    "batch_id": "20260815-000000",
    "results": [{"email": "a@example.com", "ok": True, "status": "success", "message": "ok"}],
    "ok_count": 1, "failed_count": 0,
}


def _reset_state(batch_id: str = "20260815-000001") -> None:
    with _CPA_SCAN_RELOGIN_LOCK:
        _CPA_SCAN_RELOGIN_STATE.update({
            "batch_id": batch_id,
            "running": True,
            "scanned": 0,
            "dead_total": 0,
            "reauthable": 0,
            "live": 0,
            "deactivated_mailbox": 0,
            "to_reauth": 0,
            "started": 0,
            "ok_count": 0,
            "failed_count": 0,
            "results": [],
            "logs": [],
            "current": "",
            "error": "",
        })


def _state_snapshot() -> dict:
    with _CPA_SCAN_RELOGIN_LOCK:
        return dict(_CPA_SCAN_RELOGIN_STATE)


class OnScanReloginEntryTests(unittest.TestCase):
    def tearDown(self):
        _reset_state()

    def test_scan_live_entry_updates_counts_logs_current(self):
        _reset_state("b1")
        _on_scan_relogin_entry("b1", {
            "email": "a@example.com", "status": "skipped",
            "reason": "测活判定仍可用，跳过重上", "liveness_status": "live",
        })
        s = _state_snapshot()
        self.assertEqual(s["scanned"], 1)
        self.assertEqual(s["live"], 1)
        self.assertEqual(s["reauthable"], 1)
        self.assertEqual(s["current"], "a@example.com")
        self.assertEqual(len(s["logs"]), 1)
        log = s["logs"][0]
        self.assertEqual(log["email"], "a@example.com")
        self.assertEqual(log["status"], "skipped")
        self.assertEqual(log["liveness_status"], "live")
        self.assertIn("仍可用", log["reason"])
        self.assertIn("ts", log)
        self.assertIn("liveness_error", log)

    def test_scan_deactivated_entry(self):
        _reset_state("b1")
        _on_scan_relogin_entry("b1", {
            "email": "d@example.com", "status": "deactivated_mailbox",
            "reason": "邮箱存在 OpenAI 停用邮件，判定账号已废，跳过重上",
        })
        s = _state_snapshot()
        self.assertEqual(s["scanned"], 1)
        self.assertEqual(s["deactivated_mailbox"], 1)
        self.assertEqual(s["reauthable"], 1)
        self.assertEqual(s["logs"][0]["status"], "deactivated_mailbox")

    def test_scan_to_reauth_entry(self):
        _reset_state("b1")
        _on_scan_relogin_entry("b1", {
            "email": "e@example.com", "status": "to_reauth",
            "reason": "token 失效且无停用邮件，进入重上队列",
        })
        s = _state_snapshot()
        self.assertEqual(s["scanned"], 1)
        self.assertEqual(s["to_reauth"], 1)
        self.assertEqual(s["reauthable"], 1)
        self.assertEqual(s["logs"][0]["status"], "to_reauth")

    def test_scan_unreauthable_skip_no_reauthable_count(self):
        _reset_state("b1")
        _on_scan_relogin_entry("b1", {
            "email": "f@example.com", "status": "skipped",
            "reason": "本地邮箱池无法解析取码，跳过",
        })
        s = _state_snapshot()
        self.assertEqual(s["scanned"], 1)
        self.assertEqual(s["reauthable"], 0)
        self.assertEqual(s["live"], 0)

    def test_reauth_success_entry(self):
        _reset_state("b1")
        _on_scan_relogin_entry("b1", {
            "email": "a@example.com", "ok": True, "status": "success", "message": "补跑成功",
        })
        s = _state_snapshot()
        self.assertEqual(s["started"], 1)
        self.assertEqual(s["ok_count"], 1)
        self.assertEqual(s["failed_count"], 0)
        self.assertEqual(s["logs"][0]["status"], "success")
        self.assertEqual(s["logs"][0]["reason"], "补跑成功")

    def test_reauth_failed_entry(self):
        _reset_state("b1")
        _on_scan_relogin_entry("b1", {
            "email": "b@example.com", "ok": False, "status": "failed", "message": "接码失败",
        })
        s = _state_snapshot()
        self.assertEqual(s["started"], 1)
        self.assertEqual(s["failed_count"], 1)
        self.assertEqual(s["ok_count"], 0)
        self.assertEqual(s["logs"][0]["status"], "failed")
        self.assertEqual(s["logs"][0]["reason"], "接码失败")

    def test_batch_id_mismatch_ignored(self):
        _reset_state("b1")
        _on_scan_relogin_entry("other-batch", {
            "email": "x@example.com", "status": "to_reauth", "reason": "不应写入",
        })
        s = _state_snapshot()
        self.assertEqual(s["scanned"], 0)
        self.assertEqual(s["logs"], [])
        self.assertEqual(s["current"], "")

    def test_logs_truncated_to_limit(self):
        _reset_state("b1")
        for i in range(_CPA_SCAN_RELOGIN_LOG_LIMIT + 10):
            _on_scan_relogin_entry("b1", {
                "email": f"u{i}@example.com", "status": "to_reauth", "reason": "r",
            })
        s = _state_snapshot()
        self.assertEqual(len(s["logs"]), _CPA_SCAN_RELOGIN_LOG_LIMIT)
        # 最早（最旧）的 10 条被截断；保留的是最新 200 条
        self.assertEqual(s["logs"][0]["email"], "u10@example.com")
        self.assertEqual(s["logs"][-1]["email"], f"u{_CPA_SCAN_RELOGIN_LOG_LIMIT + 9}@example.com")
        self.assertEqual(s["scanned"], _CPA_SCAN_RELOGIN_LOG_LIMIT + 10)


class PipelineCallbackTests(unittest.TestCase):
    def test_scan_pipeline_calls_callback_per_account_and_forwards_to_reauth(self):
        events = []
        liveness = [
            {"ok": False, "status": "deactivated", "error": "AT 过期"},
            {"ok": False, "status": "failed", "error": "403"},
            {"ok": False, "status": "deactivated", "error": "AT 过期"},
        ]
        mailbox = [
            {"deactivated": False, "source": "outlook"},
            {"deactivated": False, "source": "outlook"},
            {"deactivated": False, "source": "outlook"},
        ]
        with patch.object(cpa_reauth, "scan_cpa_dead_accounts", return_value=DEAD_ACCOUNTS), \
             patch.object(cpa_reauth, "is_email_reauthable", return_value=True), \
             patch("core.account_liveness.check_account_liveness", side_effect=liveness), \
             patch.object(cpa_reauth, "check_mailbox_has_deactivation", side_effect=mailbox), \
             patch.object(cpa_reauth, "run_reauth_pipeline", return_value=REAUTH_RET) as mock_reauth:
            ret = cpa_reauth.cpa_scan_relogin_pipeline(max_total=50, callback=events.append)
        # 3 个扫描阶段 entry 全部触发回调
        self.assertEqual(len(events), 3)
        self.assertEqual([e["email"] for e in events],
                         ["a@example.com", "b@example.com", "c@example.com"])
        self.assertTrue(all(e["status"] == "to_reauth" for e in events))
        # 回调透传给 run_reauth_pipeline（重上结果逐号实时走同一回调）
        args, kwargs = mock_reauth.call_args
        self.assertEqual(args[0], ["a@example.com", "b@example.com", "c@example.com"])
        self.assertEqual(kwargs["callback"], events.append)
        self.assertEqual(ret["to_reauth"], ["a@example.com", "b@example.com", "c@example.com"])

    def test_run_reauth_pipeline_calls_callback_per_result(self):
        events = []
        with patch.object(cpa_reauth, "is_email_reauthable", return_value=True), \
             patch.object(cpa_reauth.codex_retry_service, "is_retrying", return_value=False), \
             patch.object(cpa_reauth, "_run_one_reauth_with_sms_override", return_value={
                 "email": "a@example.com", "ok": True, "status": "success", "message": "ok",
             }):
            ret = cpa_reauth.run_reauth_pipeline(["a@example.com"], workers=1, callback=events.append)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["ok"])
        self.assertEqual(ret["ok_count"], 1)


if __name__ == "__main__":
    unittest.main()
