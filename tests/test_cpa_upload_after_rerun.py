# -*- coding: utf-8 -*-
"""补跑成功后主动上传 CPA auth 文件触发重载的单元测试。

背景：CPA oauth-callback 落盘只写文件、不触发内存 auth manager 重解析
（auth token not found）；只有 multipart POST /v0/management/auth-files
才会立即注册。run_worker 补跑成功（CPA 模式）后应主动 upload_cpa_auth_file
触发重载；上传失败不阻塞补跑 success。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import codex_oauth, codex_retry_service


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class _FakeSession:
    def __init__(self, status_code=200, payload=None):
        self.calls = []
        self._status_code = status_code
        self._payload = payload

    def post(self, url, headers=None, multipart=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "multipart": multipart, "timeout": timeout})
        return _FakeResponse(self._status_code, self._payload, text="err-body")

    def close(self):
        pass


class _FakeMime:
    def __init__(self):
        self.parts = []

    def addpart(self, name, *, content_type=None, filename=None, local_path=None, data=None):
        self.parts.append({"name": name, "content_type": content_type, "filename": filename, "data": data})

    def close(self):
        pass


class UploadCpaAuthFileTests(unittest.TestCase):
    def test_upload_with_email_downloads_then_posts_multipart(self):
        """只给 email 时先下载 CPA 侧最新 auth 文件，再 multipart 上传。"""
        fake_session = _FakeSession(200, {"status": "ok", "name": "codex-user@example.com-free.json"})
        fake_mime = _FakeMime()
        with patch.object(
            codex_oauth,
            "download_cpa_codex_auth_text",
            return_value=('{"access_token": "sk-test-1"}\n', "codex-user@example.com-free.json", {"name": "codex-user@example.com-free.json"}),
        ) as mock_download, patch.object(
            codex_oauth, "_cpa_management_origin", return_value="http://127.0.0.1:8317"
        ), patch.object(codex_oauth, "_cpa_management_key", return_value="test-key"), patch.object(
            codex_oauth.curl_requests, "Session", return_value=fake_session
        ), patch.object(codex_oauth, "CurlMime", return_value=fake_mime):
            result = codex_oauth.upload_cpa_auth_file(email="user@example.com")

        self.assertEqual(result, {"status": "ok", "name": "codex-user@example.com-free.json"})
        mock_download.assert_called_once_with(email="user@example.com")
        self.assertEqual(len(fake_session.calls), 1)
        call = fake_session.calls[0]
        self.assertEqual(call["url"], "http://127.0.0.1:8317/v0/management/auth-files")
        self.assertIn("Authorization", call["headers"])
        self.assertIn("X-Management-Key", call["headers"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(call["headers"]["X-Management-Key"], "test-key")
        self.assertIsNotNone(call["multipart"])
        self.assertIsNone(call["headers"].get("Content-Type"))  # multipart 由 curl 自己设置 boundary
        names = [p["name"] for p in fake_mime.parts]
        self.assertIn("name", names)
        self.assertIn("file", names)
        file_part = next(p for p in fake_mime.parts if p["name"] == "file")
        self.assertEqual(file_part["filename"], "codex-user@example.com-free.json")
        self.assertEqual(file_part["content_type"], "application/json")
        # content 经过 strip 规范化，落盘下载文本的尾部换行会被去掉
        self.assertEqual(file_part["data"], b'{"access_token": "sk-test-1"}')

    def test_upload_with_name_and_content_skips_download(self):
        """显式给 name+content 时不再下载。"""
        fake_session = _FakeSession(200, {"status": "ok"})
        fake_mime = _FakeMime()
        with patch.object(
            codex_oauth, "download_cpa_codex_auth_text"
        ) as mock_download, patch.object(
            codex_oauth, "_cpa_management_origin", return_value="http://127.0.0.1:8317"
        ), patch.object(codex_oauth, "_cpa_management_key", return_value="test-key"), patch.object(
            codex_oauth.curl_requests, "Session", return_value=fake_session
        ), patch.object(codex_oauth, "CurlMime", return_value=fake_mime):
            result = codex_oauth.upload_cpa_auth_file(
                name="codex-user@example.com-free.json",
                content='{"access_token": "sk-x"}',
            )

        self.assertEqual(result, {"status": "ok"})
        mock_download.assert_not_called()
        self.assertEqual(len(fake_session.calls), 1)

    def test_upload_missing_name_or_content_raises_valueerror(self):
        """无 email 且缺 name/content 时应报 ValueError。"""
        with self.assertRaises(ValueError):
            codex_oauth.upload_cpa_auth_file()
        with self.assertRaises(ValueError):
            codex_oauth.upload_cpa_auth_file(name="codex-x.json", content="")
        with self.assertRaises(ValueError):
            codex_oauth.upload_cpa_auth_file(name="", content="{}")

    def test_upload_non_2xx_raises_runtime_error(self):
        """非 2xx 响应抛 RuntimeError，携带 status 与错误信息。"""
        fake_session = _FakeSession(400, {"error": "bad request"})
        with patch.object(
            codex_oauth, "download_cpa_codex_auth_text",
            return_value=('{"access_token": "sk-x"}', "codex-user@example.com-free.json", {}),
        ), patch.object(
            codex_oauth, "_cpa_management_origin", return_value="http://127.0.0.1:8317"
        ), patch.object(codex_oauth, "_cpa_management_key", return_value="test-key"), patch.object(
            codex_oauth.curl_requests, "Session", return_value=fake_session
        ), patch.object(codex_oauth, "CurlMime", return_value=_FakeMime()):
            with self.assertRaises(RuntimeError) as ctx:
                codex_oauth.upload_cpa_auth_file(email="user@example.com")

        self.assertIn("status=400", str(ctx.exception))
        self.assertIn("bad request", str(ctx.exception))

    def test_upload_download_failure_propagates(self):
        """下载失败（如找不到匹配文件）时异常向上传播。"""
        with patch.object(
            codex_oauth, "download_cpa_codex_auth_text",
            side_effect=RuntimeError("[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证"),
        ):
            with self.assertRaises(RuntimeError):
                codex_oauth.upload_cpa_auth_file(email="user@example.com")


class RetryRunWorkerUploadTests(unittest.TestCase):
    """run_worker 补跑成功后按 CPA 模式触发上传，失败不阻塞。"""

    OK_RESULT = {
        "status": "success",
        "ok": True,
        "http_status": 200,
        "email": "user@example.com",
        "file_path": "/tmp/codex-user@example.com-cpa-callback.json",
        "callback_url": "http://localhost:1455/auth/callback?code=ac_test",
        "message": "",
    }

    def _run_worker(self, *, codex_source="cpa", oauth_result=None, upload_side_effect=None):
        oauth_result = oauth_result if oauth_result is not None else dict(self.OK_RESULT)
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "retry.log"
            with patch("config.reload_all", return_value=[]), \
                 patch.object(codex_retry_service.db, "update_account_codex_status", return_value=None), \
                 patch.object(codex_oauth, "run_codex_oauth", return_value=oauth_result) as mock_oauth, \
                 patch.object(config_codex(), "CODEX_AUTH_URL_SOURCE", codex_source), \
                 patch.object(codex_oauth, "upload_cpa_auth_file", side_effect=upload_side_effect) as mock_upload:
                result = codex_retry_service.run_worker(
                    "user@example.com",
                    clear_log=False,
                    target_log_path=log_file,
                )
        return result, mock_oauth, mock_upload

    def test_success_in_cpa_mode_calls_upload(self):
        """CPA 模式补跑成功 → 调用 upload_cpa_auth_file(email=email)。"""
        result, mock_oauth, mock_upload = self._run_worker(codex_source="cpa")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("status"), "success")
        mock_oauth.assert_called_once_with("user@example.com", force=True)
        mock_upload.assert_called_once_with(email="user@example.com")

    def test_success_in_non_cpa_mode_skips_upload(self):
        """非 CPA 模式（local）补跑成功 → 不调用上传。"""
        result, _mock_oauth, mock_upload = self._run_worker(codex_source="local")
        self.assertTrue(result.get("ok"))
        mock_upload.assert_not_called()

    def test_upload_failure_does_not_block_success(self):
        """上传失败 → 仅告警，补跑仍返回 success。"""
        result, _mock_oauth, mock_upload = self._run_worker(
            codex_source="cpa",
            upload_side_effect=RuntimeError("CPA 管理接口不可达"),
        )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("status"), "success")
        mock_upload.assert_called_once_with(email="user@example.com")

    def test_failed_rerun_skips_upload(self):
        """补跑失败（ok=False）→ 不调用上传。"""
        failed = dict(self.OK_RESULT)
        failed.update({"ok": False, "status": "failed", "message": "登录失败"})
        result, _mock_oauth, mock_upload = self._run_worker(codex_source="cpa", oauth_result=failed)
        self.assertFalse(result.get("ok"))
        mock_upload.assert_not_called()


def config_codex():
    import config.codex as codex_mod
    return codex_mod


if __name__ == "__main__":
    unittest.main()
