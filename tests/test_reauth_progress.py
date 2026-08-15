# -*- coding: utf-8 -*-
"""「重上失效号」实时进度回调的单元测试。

覆盖：
    - _on_reauth_entry：成功/失败计数（ok_count/failed_count）
    - _on_reauth_entry：logs 追加 + current 更新 + 日志结构（与 scan-relogin 一致）
    - _on_reauth_entry：logs 截断上限（_CPA_REAUTH_LOG_LIMIT）
    - _on_reauth_entry：batch_id 不匹配时忽略
    - POST /api/cpa/reauth/run：callback 透传给 run_reauth_pipeline，逐号实时更新状态
    - GET /api/cpa/reauth/status：返回 logs/current（前后端字段一致）
"""
import time
import unittest
from unittest.mock import patch

from webui.app import (
    _CPA_REAUTH_LOG_LIMIT,
    _CPA_REAUTH_LOCK,
    _CPA_REAUTH_STATE,
    _on_reauth_entry,
    create_app,
)


def _reset_state(batch_id: str = "20260815-000001") -> None:
    with _CPA_REAUTH_LOCK:
        _CPA_REAUTH_STATE.update({
            "batch_id": batch_id,
            "running": True,
            "ok_count": 0,
            "failed_count": 0,
            "results": [],
            "logs": [],
            "current": "",
        })


def _state_snapshot() -> dict:
    with _CPA_REAUTH_LOCK:
        return dict(_CPA_REAUTH_STATE)


class OnReauthEntryTests(unittest.TestCase):
    def tearDown(self):
        _reset_state()

    def test_success_entry_updates_counts_logs_current(self):
        _reset_state("b1")
        _on_reauth_entry("b1", {
            "email": "a@example.com", "ok": True, "status": "success", "message": "补跑成功",
        })
        s = _state_snapshot()
        self.assertEqual(s["ok_count"], 1)
        self.assertEqual(s["failed_count"], 0)
        self.assertEqual(s["current"], "a@example.com")
        self.assertEqual(len(s["logs"]), 1)
        log = s["logs"][0]
        self.assertEqual(log["email"], "a@example.com")
        self.assertEqual(log["status"], "success")
        self.assertEqual(log["reason"], "补跑成功")
        self.assertIn("ts", log)

    def test_failed_entry_updates_counts_and_reason(self):
        _reset_state("b1")
        _on_reauth_entry("b1", {
            "email": "b@example.com", "ok": False, "status": "failed",
            "message": "接码失败: no number",
        })
        s = _state_snapshot()
        self.assertEqual(s["ok_count"], 0)
        self.assertEqual(s["failed_count"], 1)
        self.assertEqual(s["logs"][0]["status"], "failed")
        self.assertEqual(s["logs"][0]["reason"], "接码失败: no number")

    def test_entry_without_ok_counts_as_failed(self):
        _reset_state("b1")
        _on_reauth_entry("b1", {"email": "c@example.com", "status": "failed", "message": "异常"})
        s = _state_snapshot()
        self.assertEqual(s["failed_count"], 1)
        self.assertEqual(s["ok_count"], 0)

    def test_reason_from_reason_field_fallback(self):
        _reset_state("b1")
        _on_reauth_entry("b1", {"email": "d@example.com", "ok": False, "reason": "旧凭证删除失败"})
        s = _state_snapshot()
        self.assertEqual(s["logs"][0]["reason"], "旧凭证删除失败")

    def test_batch_id_mismatch_ignored(self):
        _reset_state("b1")
        _on_reauth_entry("other-batch", {
            "email": "x@example.com", "ok": True, "message": "不应写入",
        })
        s = _state_snapshot()
        self.assertEqual(s["ok_count"], 0)
        self.assertEqual(s["failed_count"], 0)
        self.assertEqual(s["logs"], [])
        self.assertEqual(s["current"], "")

    def test_logs_truncated_to_limit(self):
        _reset_state("b1")
        for i in range(_CPA_REAUTH_LOG_LIMIT + 10):
            _on_reauth_entry("b1", {
                "email": f"u{i}@example.com", "ok": True, "message": "r",
            })
        s = _state_snapshot()
        self.assertEqual(len(s["logs"]), _CPA_REAUTH_LOG_LIMIT)
        self.assertEqual(s["logs"][0]["email"], "u10@example.com")
        self.assertEqual(s["logs"][-1]["email"], f"u{_CPA_REAUTH_LOG_LIMIT + 9}@example.com")
        self.assertEqual(s["ok_count"], _CPA_REAUTH_LOG_LIMIT + 10)


class ReauthWebUiProgressApiTests(unittest.TestCase):
    def setUp(self):
        _reset_state("")
        with _CPA_REAUTH_LOCK:
            _CPA_REAUTH_STATE["running"] = False
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def tearDown(self):
        with _CPA_REAUTH_LOCK:
            _CPA_REAUTH_STATE.update({
                "batch_id": "", "running": False,
                "ok_count": 0, "failed_count": 0, "results": [],
                "logs": [], "current": "",
            })

    def test_run_forwards_callback_and_status_returns_logs_current(self):
        captured = {}

        def fake_pipeline(emails, **kwargs):
            captured["callback"] = kwargs.get("callback")
            cb = kwargs.get("callback")
            if cb:
                cb({"email": "a@example.com", "ok": True, "status": "success", "message": "补跑成功"})
                cb({"email": "b@example.com", "ok": False, "status": "failed", "message": "接码失败"})
            return {"ok": True, "ok_count": 1, "failed_count": 1,
                    "results": [{"email": "a@example.com", "ok": True},
                                {"email": "b@example.com", "ok": False}]}

        with patch("core.cpa_reauth.run_reauth_pipeline", side_effect=fake_pipeline), \
             patch("core.cpa_reauth.is_email_reauthable", return_value=True), \
             patch("core.cpa_reauth.proto.list_cpa_codex_auth_files", return_value=[]), \
             patch("webui.app.codex_retry_service.is_retrying", return_value=False):
            resp = self.client.post("/api/cpa/reauth/run", json={
                "emails": ["a@example.com", "b@example.com"],
            })
        self.assertEqual(resp.status_code, 200)

        deadline = time.time() + 5
        while _state_snapshot().get("running") and time.time() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(captured.get("callback"))

        s = _state_snapshot()
        self.assertEqual(s["ok_count"], 1)
        self.assertEqual(s["failed_count"], 1)
        self.assertEqual(len(s["logs"]), 2)
        self.assertEqual(s["logs"][0]["status"], "success")
        self.assertEqual(s["logs"][1]["status"], "failed")
        self.assertEqual(s["current"], "b@example.com")

        # status 路由返回 logs/current（前后端字段一致）
        status = self.client.get("/api/cpa/reauth/status").get_json()
        self.assertTrue(status["ok"])
        self.assertIn("logs", status)
        self.assertIn("current", status)
        self.assertEqual(len(status["logs"]), 2)
        self.assertEqual(status["current"], "b@example.com")


if __name__ == "__main__":
    unittest.main()
