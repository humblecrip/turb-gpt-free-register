# -*- coding: utf-8 -*-
"""iCloud 工作流编排服务单元测试。

覆盖：
    - start() 创建 N 个 job 并入队；重复 start 返回 409
    - 排队中的 job 在 pause() 后标记 paused，resume() 恢复
    - stop() 把排队 job 标记 stopped，批处理状态结束
    - retry() 对 failed/stopped/paused job 重新入队；未知 job 404
    - 单 job 执行：领别名→注册→补跑→上传CPA 全链路（mock 依赖）
    - 断点续跑：已有 email+account_id 的 job 不重复领别名/注册
    - 别名池状态在 /api/accounts /api/aliases 可用/不可用时的降级

全部 mock 掉真实依赖（icloud_hme_client / main.run_registration /
codex_retry_service），不碰网络与浏览器。
"""
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from core import icloud_workflow_service as svc


def _reset_state():
    svc._STATE.update({
        "running": False,
        "paused": False,
        "stop_requested": False,
        "batch_id": None,
        "config": {},
        "jobs": {},
        "queue": svc.queue_mod.Queue(),
        "executor": None,
        "dispatcher_thread": None,
        "started_at": None,
        "finished_at": None,
        "last_error": None,
    })
    svc._ORIG_SMS_COUNTRY = None


def _wait_until(pred, timeout=5.0, interval=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


class IcloudWorkflowServiceStartStopTests(unittest.TestCase):
    """状态机测试：用阻塞的假 _run_job 避免真实依赖，保证确定。"""

    def setUp(self):
        _reset_state()
        self._gate = threading.Event()
        self._patchers = []

    def tearDown(self):
        self._gate.set()
        for p in self._patchers:
            p.stop()
        try:
            svc.stop()
        except Exception:
            pass
        _reset_state()

    def _install_fake_run(self, block=True):
        """安装一个假 _run_job：阻塞在 gate 上（模拟长任务）。"""
        gate = self._gate
        state = {"runs": 0}

        def fake_run(job_id):
            state["runs"] += 1
            svc._update_job(job_id, status=svc.STATUS_RUNNING)
            gate.wait(5)
            svc._update_job(job_id, status=svc.STATUS_DONE, finished_at=svc._now())

        p = patch.object(svc, "_run_job", side_effect=fake_run)
        p.start()
        self._patchers.append(p)
        return state

    def test_start_creates_jobs_and_marks_queued(self):
        self._install_fake_run(block=True)
        result = svc.start(count=3, workers=2, country="", auto_upload=True)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(len(result["jobs"]), 3)
        status = svc.status()
        self.assertTrue(status["running"])
        self.assertGreaterEqual(status["counts"]["queued"] + status["counts"]["running"], 3)
        self.assertEqual(status["batch_id"], result["batch_id"])

    def test_start_rejects_when_already_running(self):
        self._install_fake_run(block=True)
        svc.start(count=1, workers=1)
        result = svc.start(count=1, workers=1)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status"), 409)

    def test_pause_marks_queued_paused_and_resume_restores(self):
        self._install_fake_run(block=True)
        svc.start(count=4, workers=1)
        self.assertTrue(_wait_until(lambda: svc._count_running() >= 1))
        r = svc.pause()
        self.assertTrue(r.get("ok"))
        status = svc.status()
        self.assertTrue(status["paused"])
        # 1 个 running，其余应标记 paused
        self.assertGreaterEqual(status["counts"]["paused"], 3)

        r2 = svc.resume()
        self.assertTrue(r2.get("ok"))
        status2 = svc.status()
        self.assertFalse(status2["paused"])
        self.assertGreaterEqual(status2["counts"]["queued"], 3)

    def test_stop_marks_queued_stopped(self):
        self._install_fake_run(block=True)
        svc.start(count=4, workers=1)
        self.assertTrue(_wait_until(lambda: svc._count_running() >= 1))
        r = svc.stop()
        self.assertTrue(r.get("ok"))
        status = svc.status()
        self.assertGreaterEqual(status["counts"]["stopped"], 3)
        self.assertFalse(status["running"])

    def test_retry_rejects_unknown_job(self):
        r = svc.retry("nope")
        self.assertFalse(r.get("ok"))
        self.assertEqual(r.get("status"), 404)

    def test_retry_stopped_job_reenqueues(self):
        # 非阻塞 fake_run：任务立即完成 → 批次结束后手动置为 failed → 重试应重建 dispatcher 并再次执行
        runs = {"n": 0}

        def fake_run(job_id):
            runs["n"] += 1
            svc._update_job(job_id, status=svc.STATUS_DONE, finished_at=svc._now())

        p = patch.object(svc, "_run_job", side_effect=fake_run)
        p.start()
        self._patchers.append(p)

        svc.start(count=1, workers=1)
        job_id = list(svc._STATE["jobs"].keys())[0]
        self.assertTrue(_wait_until(lambda: svc._get_job(job_id).get("status") == svc.STATUS_DONE))
        # 批次已结束（dispatcher 退出），把 job 置为 failed 模拟失败号
        self.assertTrue(_wait_until(lambda: not svc.status()["running"]))
        svc._update_job(job_id, status=svc.STATUS_FAILED, error="test")

        r = svc.retry(job_id)
        self.assertTrue(r.get("ok"), r)
        # 重试会重建 dispatcher 并再次执行，最终回到 done
        self.assertTrue(_wait_until(lambda: runs["n"] >= 2))
        self.assertTrue(_wait_until(lambda: svc._get_job(job_id).get("status") == svc.STATUS_DONE))
        job = svc._get_job(job_id)
        self.assertIsNone(job.get("error"))


class IcloudWorkflowServiceJobRunTests(unittest.TestCase):
    """用 mock 依赖直接驱动 _run_job，验证 领别名→注册→补跑→上传 链路。"""

    def setUp(self):
        _reset_state()
        self.email = "alias123@icloud.com"

    def tearDown(self):
        _reset_state()

    def test_job_runs_full_flow_and_done(self):
        fake_acc = MagicMock()
        fake_acc.email = self.email
        reg_result = {"success": True, "email": self.email, "account_id": 42, "codex": {"status": "failed"}}
        codex_result = {"status": "success", "ok": True, "message": "ok"}

        job_id = "testjob1"
        svc._STATE["jobs"][job_id] = svc._new_job()

        with patch("config.email.USE_EMAIL_SERVICE", True), \
             patch("core.icloud_hme_client.pick_account", return_value=fake_acc), \
             patch("main.run_registration", return_value=reg_result), \
             patch("core.codex_retry_service.reserve", return_value=True), \
             patch("core.codex_retry_service.run_worker", return_value=codex_result), \
             patch("core.codex_oauth.upload_cpa_auth_file", return_value={"status": "ok"}):
            svc._STATE["config"] = {"auto_upload": True, "workers": 1, "country": "", "count": 1}
            svc._run_job(job_id)

        job = svc._get_job(job_id)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["email"], self.email)
        self.assertEqual(job["account_id"], 42)
        self.assertEqual(job["step_label"], "上传CPA")
        log_text = "\n".join(l["message"] for l in job["logs"])
        self.assertIn("已领取别名", log_text)
        self.assertIn("注册成功", log_text)
        self.assertIn("Codex 补跑成功", log_text)
        self.assertIn("CPA auth 文件已上传", log_text)

    def test_job_registration_failed_marks_failed(self):
        fake_acc = MagicMock()
        fake_acc.email = self.email
        reg_result = {"success": False, "error": "注册被拒"}
        job_id = "testjob2"
        svc._STATE["jobs"][job_id] = svc._new_job()
        svc._STATE["config"] = {"auto_upload": True, "workers": 1, "country": "", "count": 1}

        with patch("config.email.USE_EMAIL_SERVICE", True), \
             patch("core.icloud_hme_client.pick_account", return_value=fake_acc), \
             patch("main.run_registration", return_value=reg_result):
            svc._run_job(job_id)

        job = svc._get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("注册被拒", job["error"] or "")

    def test_job_guard_blocks_when_use_email_service_false(self):
        """USE_EMAIL_SERVICE=False 时给出可操作错误，而不是 input() 阻塞后台线程。"""
        fake_acc = MagicMock()
        fake_acc.email = self.email
        job_id = "testjob5"
        svc._STATE["jobs"][job_id] = svc._new_job()
        svc._STATE["config"] = {"auto_upload": True, "workers": 1, "country": "", "count": 1}

        with patch("config.email.USE_EMAIL_SERVICE", False), \
             patch("core.icloud_hme_client.pick_account", return_value=fake_acc), \
             patch("main.run_registration", side_effect=AssertionError("不应调用注册")):
            svc._run_job(job_id)

        job = svc._get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("USE_EMAIL_SERVICE=False", job["error"] or "")

    def test_job_skips_register_if_alias_and_account_exist(self):
        """断点续跑：已有 email + account_id 的 job 直接进入补跑，不重复领别名/注册。"""
        codex_result = {"status": "success", "ok": True, "message": "ok"}
        job_id = "testjob3"
        svc._STATE["jobs"][job_id] = svc._new_job()
        svc._update_job(job_id, email=self.email, account_id=99)
        svc._STATE["config"] = {"auto_upload": True, "workers": 1, "country": "", "count": 1}

        with patch("core.icloud_hme_client.pick_account", side_effect=AssertionError("不应再次领别名")), \
             patch("main.run_registration", side_effect=AssertionError("不应再次注册")), \
             patch("core.codex_retry_service.reserve", return_value=True), \
             patch("core.codex_retry_service.run_worker", return_value=codex_result), \
             patch("core.codex_oauth.upload_cpa_auth_file", return_value={"status": "ok"}):
            svc._run_job(job_id)

        job = svc._get_job(job_id)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["account_id"], 99)
        log_text = "\n".join(l["message"] for l in job["logs"])
        self.assertIn("Codex 补跑成功", log_text)

    def test_job_skips_upload_when_auto_upload_false(self):
        fake_acc = MagicMock()
        fake_acc.email = self.email
        reg_result = {"success": True, "email": self.email, "account_id": 1, "codex": {"status": "failed"}}
        codex_result = {"status": "success", "ok": True, "message": "ok"}
        job_id = "testjob4"
        svc._STATE["jobs"][job_id] = svc._new_job()
        svc._STATE["config"] = {"auto_upload": False, "workers": 1, "country": "", "count": 1}

        with patch("core.icloud_hme_client.pick_account", return_value=fake_acc), \
             patch("main.run_registration", return_value=reg_result), \
             patch("core.codex_retry_service.reserve", return_value=True), \
             patch("core.codex_retry_service.run_worker", return_value=codex_result), \
             patch("core.codex_oauth.upload_cpa_auth_file", side_effect=AssertionError("不应上传")) as up:
            svc._run_job(job_id)
            up.assert_not_called()

        job = svc._get_job(job_id)
        self.assertEqual(job["status"], "done")
        log_text = "\n".join(l["message"] for l in job["logs"])
        self.assertIn("未勾选自动上传 CPA", log_text)


class IcloudWorkflowAliasPoolStatusTests(unittest.TestCase):
    def setUp(self):
        _reset_state()

    def test_pool_status_with_api(self):
        accounts = [
            {"id": "acc_1", "alias_total": 15, "alias_active": 12},
            {"id": "acc_2", "alias_total": 8, "alias_active": 8},
        ]
        # created_at 用当天动态生成，避免硬编码日期跨天失败（today_created 按当天比对）。
        # 保持原语义：每账号返回的同一 aliases 列表里今天创建 1 条（a），2 个账号 → 2。
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        aliases = {
            "aliases": [
                {"email": "a@icloud.com", "created_at": f"{today}T10:00:00Z"},
                {"email": "b@icloud.com", "created_at": f"{yesterday}T10:00:00Z"},
            ]
        }
        with patch("core.icloud_hme_client._request", side_effect=[accounts, aliases, aliases]) as mock_req:
            pool = svc.alias_pool_status()
        self.assertTrue(pool["ok"])
        self.assertEqual(pool["accounts"], 2)
        self.assertEqual(pool["total_aliases"], 23)
        self.assertEqual(pool["remaining"], 20)
        self.assertEqual(pool["today_created"], 2)
        self.assertEqual(mock_req.call_count, 3)

    def test_pool_status_api_unavailable(self):
        with patch("core.icloud_hme_client._request", side_effect=svc.ICloudWorkflowError("服务不可用")):
            pool = svc.alias_pool_status()
        self.assertFalse(pool["ok"])
        self.assertIn("服务不可用", pool["error"] or "")

    def test_pool_status_aliases_unavailable_falls_back_local_today(self):
        accounts = [{"id": "acc_1", "alias_total": 15, "alias_active": 12}]
        with patch("core.icloud_hme_client._request", side_effect=[accounts, svc.ICloudWorkflowError("no aliases")]):
            pool = svc.alias_pool_status()
        self.assertTrue(pool["ok"])
        self.assertEqual(pool["accounts"], 1)
        self.assertEqual(pool["total_aliases"], 15)
        self.assertEqual(pool["today_created"], svc.local_today_created())
        self.assertEqual(pool["source"], "api+local")


if __name__ == "__main__":
    unittest.main()
