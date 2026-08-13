# -*- coding: utf-8 -*-
"""补跑/重跑入口按次接码国家选择单元测试。

覆盖：
    - retry_job 传 sms_country/sms_sort → 写入 _JOB_OPTIONS（codex 与 registration 两种 action）
    - retry_job 不带 sms 参数 → 不写 _JOB_OPTIONS（向后兼容，走全局配置）
    - retry_job 非法 sms_sort → 400 错误返回
    - _run_codex_retry_job 应用 _JOB_OPTIONS 的接码覆盖，任务结束清理
    - cpa_reauth.run_reauth_pipeline 的 worker 线程应用接码覆盖，结束后清理
    - WebUI 5 个入口 API 解析/校验 sms_country / sms_sort 并下发
"""
import threading
import time
import unittest
from unittest.mock import patch

from config import codex as codex_config
from core import cpa_reauth
from core import registration_service
from core import sms_provider
from webui.app import create_app
from webui.app import _CPA_REAUTH_LOCK, _CPA_REAUTH_STATE


def _default_job(job_id: int) -> dict:
    return {"id": job_id, "log_file": "/tmp/retry_sms_test.log", "status": "pending"}


class RetryJobSmsOptionsTests(unittest.TestCase):
    def tearDown(self):
        sms_provider.clear_task_sms_override()
        registration_service._JOB_OPTIONS.clear()
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()
        try:
            delattr(registration_service._THREAD_CTX, "job_id")
        except AttributeError:
            pass

    def test_retry_job_codex_writes_sms_options(self):
        source = {"id": 100, "status": "failed", "email": "a@test.dev", "account_id": 5}
        account = {"id": 5, "email": "a@test.dev", "codex_status": "failed"}
        retry_record = {"id": 200, "log_file": "/tmp/t.log", "status": "pending",
                        "email": "a@test.dev", "account_id": 5}
        with patch.object(registration_service.db, "get_job", return_value=source), \
             patch.object(registration_service.db, "get_account", return_value=account), \
             patch.object(registration_service.db, "get_successful_retry_for_job", return_value=None), \
             patch.object(registration_service.db, "create_retry_job", return_value=(retry_record, True)), \
             patch.object(registration_service.db, "update_account_codex_status"), \
             patch.object(registration_service, "get_executor"), \
             patch.object(registration_service.codex_retry_service, "reserve", return_value=True):
            result = registration_service.retry_job(100, workers=2, sms_country="54", sms_sort="auto_price")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("retry_action"), "codex")
        self.assertEqual(registration_service._JOB_OPTIONS.get(200), {
            "email_source": None,
            "sms_country": "54",
            "sms_sort": "auto_price",
        })

    def test_retry_job_registration_writes_sms_options(self):
        source = {"id": 101, "status": "failed", "email": "", "account_id": None}
        retry_record = {"id": 201, "log_file": "/tmp/t.log", "status": "pending"}
        with patch.object(registration_service.db, "get_job", return_value=source), \
             patch.object(registration_service.db, "get_successful_retry_for_job", return_value=None), \
             patch.object(registration_service.db, "get_account", return_value=None), \
             patch.object(registration_service.db, "get_account_by_email", return_value=None), \
             patch.object(registration_service.db, "create_retry_job", return_value=(retry_record, True)), \
             patch.object(registration_service, "get_executor"):
            result = registration_service.retry_job(101, workers=1, sms_country="73")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("retry_action"), "registration")
        self.assertEqual(registration_service._JOB_OPTIONS.get(201), {
            "email_source": None,
            "sms_country": "73",
            "sms_sort": None,
        })

    def test_retry_job_without_sms_params_keeps_old_shape(self):
        source = {"id": 102, "status": "failed", "email": "", "account_id": None}
        retry_record = {"id": 202, "log_file": "/tmp/t.log", "status": "pending"}
        with patch.object(registration_service.db, "get_job", return_value=source), \
             patch.object(registration_service.db, "get_successful_retry_for_job", return_value=None), \
             patch.object(registration_service.db, "get_account", return_value=None), \
             patch.object(registration_service.db, "get_account_by_email", return_value=None), \
             patch.object(registration_service.db, "create_retry_job", return_value=(retry_record, True)), \
             patch.object(registration_service, "get_executor"):
            result = registration_service.retry_job(102, workers=1)
        self.assertTrue(result.get("ok"))
        # 不带 sms 参数 → 不写 _JOB_OPTIONS，worker 走全局配置
        self.assertNotIn(202, registration_service._JOB_OPTIONS)

    def test_retry_job_rejects_invalid_sms_sort(self):
        source = {"id": 103, "status": "failed", "email": "", "account_id": None}
        with patch.object(registration_service.db, "get_job", return_value=source):
            result = registration_service.retry_job(103, sms_sort="bogus")
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status"), 400)


class RunCodexRetryJobSmsOverrideTests(unittest.TestCase):
    def tearDown(self):
        sms_provider.clear_task_sms_override()
        registration_service._JOB_OPTIONS.clear()
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()
        try:
            delattr(registration_service._THREAD_CTX, "job_id")
        except AttributeError:
            pass

    def test_run_codex_retry_job_applies_sms_override(self):
        captured = {}

        def fake_run_worker(email, **kwargs):
            captured["queue"] = sms_provider.resolve_country_queue()
            return {"status": "success", "ok": True, "message": ""}

        with patch.object(registration_service.db, "get_job", return_value=_default_job(300)), \
             patch.object(registration_service.db, "update_job"), \
             patch.object(registration_service.codex_retry_service, "run_worker", side_effect=fake_run_worker), \
             patch.object(registration_service.codex_retry_service, "release"), \
             patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            registration_service._JOB_OPTIONS[300] = {
                "email_source": None,
                "sms_country": "54",
                "sms_sort": "auto_price",
            }
            registration_service._run_codex_retry_job(300, "/tmp/t.log", "a@test.dev", 5)
        # worker 线程内队列被覆盖：54 前置到队列头
        self.assertEqual(captured["queue"][0], "54")
        # 任务结束后清理：_JOB_OPTIONS 与线程覆盖均不残留
        self.assertNotIn(300, registration_service._JOB_OPTIONS)
        with patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            self.assertEqual(sms_provider.resolve_country_queue(), ["73", "33"])


class ReauthPipelineSmsOverrideTests(unittest.TestCase):
    def tearDown(self):
        sms_provider.clear_task_sms_override()

    def test_reauth_pipeline_worker_applies_sms_override(self):
        captured = {}

        def fake_reauth(email, **kwargs):
            captured["email"] = email
            captured["queue"] = sms_provider.resolve_country_queue()
            return {"email": email, "ok": True, "status": "success", "message": ""}

        with patch.object(cpa_reauth, "is_email_reauthable", return_value=True), \
             patch.object(cpa_reauth.codex_retry_service, "is_retrying", return_value=False), \
             patch.object(cpa_reauth, "_run_one_reauth", side_effect=fake_reauth), \
             patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            ret = cpa_reauth.run_reauth_pipeline(["a@test.dev"], workers=1, sms_country="54")
        self.assertEqual(ret.get("ok_count"), 1)
        self.assertEqual(captured["queue"], ["54", "73", "33"])
        # worker 结束后覆盖已清理，不跨线程泄漏
        with patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            self.assertEqual(sms_provider.resolve_country_queue(), ["73", "33"])

    def test_reauth_pipeline_without_sms_params_keeps_global_queue(self):
        captured = {}

        def fake_reauth(email, **kwargs):
            captured["queue"] = sms_provider.resolve_country_queue()
            return {"email": email, "ok": True, "status": "success", "message": ""}

        with patch.object(cpa_reauth, "is_email_reauthable", return_value=True), \
             patch.object(cpa_reauth.codex_retry_service, "is_retrying", return_value=False), \
             patch.object(cpa_reauth, "_run_one_reauth", side_effect=fake_reauth), \
             patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            cpa_reauth.run_reauth_pipeline(["a@test.dev"], workers=1)
        self.assertEqual(captured["queue"], ["73", "33"])


class WebUiRetrySmsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def tearDown(self):
        sms_provider.clear_task_sms_override()
        registration_service._JOB_OPTIONS.clear()
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()
        with _CPA_REAUTH_LOCK:
            _CPA_REAUTH_STATE.update({
                "batch_id": "", "running": False,
                "ok_count": 0, "failed_count": 0, "results": [],
            })

    @patch("webui.app.svc.retry_job", return_value={"ok": True, "job": {}})
    def test_job_retry_passes_sms_params(self, retry_job):
        resp = self.client.post("/api/jobs/55/retry", json={
            "workers": 2, "sms_country": "54", "sms_sort": "auto_price",
        })
        self.assertEqual(resp.status_code, 200)
        retry_job.assert_called_once_with(55, workers=2, sms_country="54", sms_sort="auto_price")

    @patch("webui.app.svc.retry_job", return_value={"ok": True, "job": {}})
    def test_job_retry_without_sms_params_keeps_old_call(self, retry_job):
        resp = self.client.post("/api/jobs/55/retry", json={"workers": 1})
        self.assertEqual(resp.status_code, 200)
        retry_job.assert_called_once_with(55, workers=1, sms_country=None, sms_sort=None)

    @patch("webui.app.svc.retry_job")
    def test_job_retry_rejects_invalid_sms_sort(self, retry_job):
        resp = self.client.post("/api/jobs/55/retry", json={"sms_sort": "bogus"})
        self.assertEqual(resp.status_code, 400)
        retry_job.assert_not_called()

    @patch("webui.app.svc.retry_job", return_value={"ok": True, "job": {}})
    def test_jobs_retry_bulk_passes_sms_params(self, retry_job):
        resp = self.client.post("/api/jobs/retry-bulk", json={
            "job_ids": [1, 2], "sms_country": "73", "sms_sort": "manual",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(retry_job.call_args_list), 2)
        for call in retry_job.call_args_list:
            self.assertEqual(call.kwargs.get("sms_country"), "73")
            self.assertEqual(call.kwargs.get("sms_sort"), "manual")

    @patch("webui.app.svc.retry_job")
    def test_jobs_retry_bulk_rejects_invalid_sms_sort(self, retry_job):
        resp = self.client.post("/api/jobs/retry-bulk", json={"job_ids": [1], "sms_sort": "bogus"})
        self.assertEqual(resp.status_code, 400)
        retry_job.assert_not_called()

    @patch("webui.app.codex_retry_service.run_worker")
    @patch("webui.app.codex_retry_service.reserve", return_value=True)
    @patch("webui.app.db.update_account_codex_status")
    @patch("webui.app.db.get_account_by_email",
           return_value={"id": 1, "email": "a@test.dev", "codex_status": "failed"})
    def test_codex_retry_passes_sms_params(self, get_account, update_status, reserve, run_worker):
        captured = {}

        def fake_run_worker(email, **kwargs):
            captured["queue"] = sms_provider.resolve_country_queue()

        run_worker.side_effect = fake_run_worker
        resp = self.client.post("/api/codex/retry", json={
            "email": "a@test.dev", "sms_country": "54", "sms_sort": "",
        })
        self.assertEqual(resp.status_code, 200)
        deadline = time.time() + 3
        while not run_worker.called and time.time() < deadline:
            time.sleep(0.01)
        run_worker.assert_called_once()
        self.assertEqual(run_worker.call_args.args[0], "a@test.dev")
        # 补跑 worker 线程内覆盖生效：54 前置到队列头
        self.assertEqual(captured["queue"][0], "54")

    @patch("webui.app.codex_retry_service.run_worker")
    @patch("webui.app.codex_retry_service.reserve", return_value=True)
    @patch("webui.app.db.update_account_codex_status")
    @patch("webui.app.db.get_account_by_email",
           return_value={"id": 1, "email": "a@test.dev", "codex_status": "failed"})
    def test_codex_retry_rejects_invalid_sms_sort(self, get_account, update_status, reserve, run_worker):
        resp = self.client.post("/api/codex/retry", json={"email": "a@test.dev", "sms_sort": "bogus"})
        self.assertEqual(resp.status_code, 400)
        run_worker.assert_not_called()

    @patch("core.cpa_reauth.run_reauth_pipeline",
           return_value={"ok": True, "ok_count": 1, "failed_count": 0, "results": []})
    @patch("core.cpa_reauth.is_email_reauthable", return_value=True)
    @patch("core.cpa_reauth.proto.list_cpa_codex_auth_files", return_value=[])
    @patch("webui.app.codex_retry_service.is_retrying", return_value=False)
    def test_cpa_reauth_run_passes_sms_params(self, is_retrying, list_files, is_reauthable, pipeline):
        resp = self.client.post("/api/cpa/reauth/run", json={
            "emails": ["a@test.dev"], "sms_country": "54", "sms_sort": "auto_success",
        })
        self.assertEqual(resp.status_code, 200)
        deadline = time.time() + 3
        while not pipeline.called and time.time() < deadline:
            time.sleep(0.01)
        pipeline.assert_called_once()
        kwargs = pipeline.call_args.kwargs
        self.assertEqual(kwargs["sms_country"], "54")
        self.assertEqual(kwargs["sms_sort"], "auto_success")

    @patch("core.cpa_reauth.run_reauth_pipeline")
    def test_cpa_reauth_run_rejects_invalid_sms_sort(self, pipeline):
        resp = self.client.post("/api/cpa/reauth/run", json={"emails": ["a@test.dev"], "sms_sort": "bogus"})
        self.assertEqual(resp.status_code, 400)
        pipeline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
