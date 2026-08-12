# -*- coding: utf-8 -*-
"""注册页邮箱源选择 + 接码国家优先级单元测试。

覆盖：
    - email_provider.acquire_email(source=...) 只从指定源领取，失败不兜底其他源
    - registration_service.submit_registration 按次 email_source / sms_country / sms_sort 参数
    - worker 领取邮箱使用任务指定的邮箱源（_THREAD_CTX.email_source）
    - sms_provider 线程级任务覆盖（set_task_sms_override / clear_task_sms_override）
    - 任务结束恢复（_deactivate_job 清理线程覆盖与任务选项）
    - WebUI POST /api/jobs 解析/校验 email_source / sms_country / sms_sort
"""
import tempfile
import unittest
from unittest.mock import patch

from config import email as email_config
from config import register as register_config
from config import codex as codex_config
from core import email_provider
from core import sms_provider
from core import registration_service
from webui.app import create_app

_PRICES = {"54": 2.5, "73": 1.0, "76": 0.5, "33": 1.5}
_STATUS = {"54": 100, "73": 50, "33": 30}
_RATES = {"54": 0.9, "73": 0.8, "33": 0.6}


class AcquireEmailSpecifiedSourceTests(unittest.TestCase):
    def tearDown(self):
        # 避免线程本地残留影响其他用例
        try:
            delattr(registration_service._THREAD_CTX, "email_source")
        except AttributeError:
            pass
        sms_provider.clear_task_sms_override()
        registration_service._JOB_OPTIONS.clear()

    @patch("core.gptmail_client.pick_account")
    def test_acquire_email_specified_gptmail_source(self, pick_account):
        pick_account.return_value.email = "fresh@gptmail.test"
        self.assertEqual(email_provider.acquire_email(source="gptmail"), "fresh@gptmail.test")
        pick_account.assert_called_once()

    @patch("core.icloud_hme_client.pick_account")
    def test_acquire_email_specified_icloud_hme_source(self, pick_account):
        pick_account.return_value.email = "alias@icloud.test"
        self.assertEqual(email_provider.acquire_email(source="icloud_hme"), "alias@icloud.test")
        pick_account.assert_called_once()

    @patch("core.gptmail_client.pick_account", side_effect=RuntimeError("no gptmail accounts"))
    @patch("core.outlook_client.pick_account")
    def test_acquire_email_specified_source_does_not_fallback(self, outlook_pick, gptmail_pick):
        # 指定 gptmail 失败时直接报错，不兜底到 outlook
        with self.assertRaises(RuntimeError) as ctx:
            email_provider.acquire_email(source="gptmail")
        self.assertIn("gptmail", str(ctx.exception))
        outlook_pick.assert_not_called()

    def test_acquire_email_invalid_source_raises(self):
        with self.assertRaises(RuntimeError):
            email_provider.acquire_email(source="bogus")

    @patch("core.gptmail_client.pick_account")
    def test_acquire_email_default_still_follows_config_order(self, pick_account):
        pick_account.return_value.email = "fresh@gptmail.test"
        with patch("core.email_provider.parse_email_sources", return_value=["gptmail"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@gptmail.test")


class SubmitRegistrationParamsTests(unittest.TestCase):
    def tearDown(self):
        sms_provider.clear_task_sms_override()
        registration_service._JOB_OPTIONS.clear()
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()

    @patch("core.registration_service.db.get_job", return_value=None)
    @patch("core.registration_service.db.create_job",
           return_value={"id": 9001, "log_file": "/tmp/x.log", "status": "pending"})
    @patch("core.registration_service.get_executor")
    def test_submit_registration_stores_per_job_options(self, get_executor, create_job, get_job):
        executor = get_executor.return_value
        jobs = registration_service.submit_registration(
            count=1, workers=2, email_source="icloud_hme",
            sms_country="54", sms_sort="auto_price",
        )
        executor.submit.assert_called_once()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(registration_service._JOB_OPTIONS.get(9001), {
            "email_source": "icloud_hme",
            "sms_country": "54",
            "sms_sort": "auto_price",
        })

    def test_submit_registration_rejects_invalid_sms_sort(self):
        with self.assertRaises(ValueError):
            registration_service.submit_registration(count=1, sms_sort="bogus")

    @patch("core.registration_service.db.create_job",
           return_value={"id": 9002, "log_file": "/tmp/x.log", "status": "pending"})
    def test_submit_registration_sms_sort_empty_normalized(self, create_job):
        # sms_sort 传入空串/None 等价，不触发 ValueError
        registration_service.submit_registration(count=1, sms_sort="", sms_country="73")

    @patch("core.registration_service.db.get_job", return_value=None)
    @patch("core.registration_service.db.create_job",
           return_value={"id": 9003, "log_file": "/tmp/x.log", "status": "pending"})
    @patch("core.registration_service.get_executor")
    def test_submit_registration_default_email_source_from_config(self, get_executor, create_job, get_job):
        with patch.object(email_config, "EMAIL_SOURCE", "outlook"):
            registration_service.submit_registration(count=1, workers=1)
        self.assertEqual(registration_service._JOB_OPTIONS.get(9003)["email_source"], "outlook")

    @patch("core.registration_service.db.create_job",
           return_value={"id": 9004, "log_file": "/tmp/x.log", "status": "pending"})
    @patch("core.registration_service.get_executor")
    def test_submit_registration_multi_source_config_not_used_as_single_override(self, get_executor, create_job):
        # 默认多来源配置（icloud_hme,outlook）不能当作"指定单一来源"下发 worker，
        # 否则 acquire_email(source='icloud_hme,outlook') 会报"不支持的邮箱来源"
        with patch.object(email_config, "EMAIL_SOURCE", "icloud_hme,outlook"):
            registration_service.submit_registration(count=1, workers=1)
        self.assertIsNone(registration_service._JOB_OPTIONS.get(9004)["email_source"])
        # DB 记录仍保留原始多来源值，用于任务溯源
        create_job.assert_called_once()
        self.assertEqual(create_job.call_args.kwargs.get("email_source"), "icloud_hme,outlook")


class WorkerEmailSourceTests(unittest.TestCase):
    def tearDown(self):
        try:
            delattr(registration_service._THREAD_CTX, "email_source")
        except AttributeError:
            pass
        sms_provider.clear_task_sms_override()
        registration_service._JOB_OPTIONS.clear()
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()

    def test_prepare_registration_args_uses_thread_email_source(self):
        registration_service._THREAD_CTX.email_source = "gptmail"
        captured = {}
        def fake_prepare():
            captured["email_source"] = getattr(registration_service._THREAD_CTX, "email_source", None)
            return ("email@test.dev", "Test User", "1990-01-01")
        with patch.object(registration_service, "_prepare_registration_args", side_effect=fake_prepare), \
             patch("core.registration_service.db.get_job",
                   return_value={"id": 7, "status": "pending", "log_file": "/tmp/t.log"}), \
             patch("core.registration_service.db.update_job"), \
             patch.object(registration_service, "_JobLogContext", return_value=_NullCtx()), \
             patch("main.run_registration", return_value={"success": True, "email": "email@test.dev"}):
            registration_service._JOB_OPTIONS[7] = {
                "email_source": "gptmail",
                "sms_country": "54",
                "sms_sort": "auto_price",
            }
            registration_service._run_one_job(7, "/tmp/t.log")
        self.assertEqual(captured["email_source"], "gptmail")
        # 任务结束后清理：邮箱源、接码覆盖、任务选项均不残留
        self.assertFalse(hasattr(registration_service._THREAD_CTX, "email_source"))
        self.assertNotIn(7, registration_service._JOB_OPTIONS)
        with patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            self.assertEqual(sms_provider.resolve_country_queue(), ["73", "33"])


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class SmsTaskOverrideTests(unittest.TestCase):
    def tearDown(self):
        sms_provider.clear_task_sms_override()

    def test_task_country_override_prepends_to_queue(self):
        sms_provider.set_task_sms_override("54", None)
        try:
            with patch.object(codex_config, "SMS_COUNTRY", "73"), \
                 patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
                self.assertEqual(sms_provider.resolve_country_queue(), ["54", "73", "33"])
        finally:
            sms_provider.clear_task_sms_override()
        with patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            self.assertEqual(sms_provider.resolve_country_queue(), ["73", "33"])

    def test_task_sort_override_wins_over_config(self):
        sms_provider.set_task_sms_override(None, "auto_price")
        try:
            with patch.object(codex_config, "SMS_COUNTRY_SORT", "manual"), \
                 patch.object(sms_provider, "_fetch_price_info", return_value=(_PRICES, _STATUS)), \
                 patch.object(sms_provider, "local_country_success_rates", return_value=_RATES), \
                 patch.object(sms_provider, "_top_countries_from_api", return_value=[]):
                self.assertEqual(sms_provider.resolve_country_queue(), ["73", "33", "54"])
        finally:
            sms_provider.clear_task_sms_override()

    def test_task_override_does_not_leak_to_other_thread(self):
        sms_provider.set_task_sms_override("54", None)
        results = []

        def in_other_thread():
            results.append(sms_provider.resolve_country_queue())

        import threading
        t = threading.Thread(target=in_other_thread)
        t.start()
        t.join()
        sms_provider.clear_task_sms_override()
        with patch.object(codex_config, "SMS_COUNTRY", "73"), \
             patch.object(codex_config, "SMS_FALLBACK_COUNTRIES", "33"):
            self.assertEqual(results[0], ["73", "33"])


class WebUiJobCreateTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def tearDown(self):
        sms_provider.clear_task_sms_override()
        registration_service._JOB_OPTIONS.clear()

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_parses_email_source_and_sms_params(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), \
             patch.object(email_config, "EMAIL_SOURCE", "outlook"), \
             patch.object(email_config, "GPTMAIL_API_KEY", "key-123"):
            resp = self.client.post("/api/jobs", json={
                "count": 1, "workers": 2,
                "email_source": "gptmail", "sms_country": "54", "sms_sort": "auto_price",
            })
        self.assertEqual(resp.status_code, 200)
        submit_registration.assert_called_once_with(
            count=1, workers=2, email_source="gptmail", sms_country="54", sms_sort="auto_price"
        )

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_without_extra_params_keeps_old_call_shape(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), \
             patch.object(email_config, "EMAIL_SOURCE", "outlook"), \
             patch("webui.app.db.outlook_pool_summary",
                   return_value={"total": 1, "available": 1, "used": 0, "failed": 0}):
            resp = self.client.post("/api/jobs", json={"count": 1, "workers": 1})
        self.assertEqual(resp.status_code, 200)
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_invalid_email_source(self, submit_registration):
        resp = self.client.post("/api/jobs", json={"count": 1, "workers": 1, "email_source": "bogus"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不支持的邮箱来源", resp.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_invalid_sms_sort(self, submit_registration):
        resp = self.client.post("/api/jobs", json={"count": 1, "workers": 1, "sms_sort": "bogus"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不支持的接码排序策略", resp.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_gptmail_source_without_api_key(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), \
             patch.object(email_config, "GPTMAIL_API_KEY", ""):
            resp = self.client.post("/api/jobs", json={"count": 1, "workers": 1, "email_source": "gptmail"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("请填写 GPTMail API Key", resp.get_json()["error"])
        submit_registration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
