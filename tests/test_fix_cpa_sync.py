# -*- coding: utf-8 -*-
"""补跑成功后 CPA 同步修复的单元测试。

背景：补跑成功后 upload_cpa_auth_file(email=...) 原来从 CPA 下载现有文件再上传，
CPA 里是失效版就永远修不好（死循环）。修复后应优先从本地账号库取有效
access_token 构造 CPA 兼容凭证上传；账号库无 token 才回退下载。同时
codex-{email}.json 有效凭证应真实落盘到 codex_accounts/。
"""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import codex_oauth
from core import db as core_db


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_jwt(payload: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    return (
        f"{_b64url(json.dumps(header).encode())}."
        f"{_b64url(json.dumps(payload).encode())}."
        "signature"
    )


def _payload_with_account(account_id="a03cd290-f321-4704-9984-3a05633d610c", exp=1787282139):
    return {
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        "sub": "auth0|fallback",
        "exp": exp,
    }


class BuildCredentialFromAccountTests(unittest.TestCase):
    def test_extracts_account_id_and_expired_format(self):
        token = _make_jwt(_payload_with_account())
        cred = codex_oauth.build_credential_from_account("user@example.com", token)

        self.assertEqual(cred["type"], "codex")
        self.assertEqual(cred["email"], "user@example.com")
        self.assertEqual(cred["account_id"], "a03cd290-f321-4704-9984-3a05633d610c")
        self.assertFalse(cred["disabled"])
        self.assertEqual(cred["id_token"], token)
        self.assertEqual(cred["access_token"], token)
        self.assertEqual(cred["refresh_token"], "")
        # exp=1787282139 → 2026-08-21T11:15:39+08:00
        self.assertEqual(cred["expired"], "2026-08-21T11:15:39+08:00")
        # last_refresh 也应是 +08:00 格式
        self.assertRegex(cred["last_refresh"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")

    def test_falls_back_to_sub_when_auth_claim_missing(self):
        payload = {"sub": "auth0|fallback", "exp": 1787282139}
        cred = codex_oauth.build_credential_from_account("user@example.com", _make_jwt(payload))
        self.assertEqual(cred["account_id"], "auth0|fallback")

    def test_empty_token_returns_empty_account_id_and_expired(self):
        cred = codex_oauth.build_credential_from_account("user@example.com", "")
        self.assertEqual(cred["account_id"], "")
        self.assertEqual(cred["expired"], "")
        self.assertEqual(cred["access_token"], "")

    def test_missing_exp_returns_empty_expired(self):
        payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "abc"}}
        cred = codex_oauth.build_credential_from_account("user@example.com", _make_jwt(payload))
        self.assertEqual(cred["account_id"], "abc")
        self.assertEqual(cred["expired"], "")


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


class UploadPrefersAccountTokenTests(unittest.TestCase):
    def _run_upload(self, account, *, email="user@example.com", download_return=None):
        fake_session = _FakeSession(200, {"status": "ok"})
        fake_mime = _FakeMime()
        with patch.object(core_db, "get_account_by_email", return_value=account) as mock_get, \
             patch.object(codex_oauth, "download_cpa_codex_auth_text", return_value=download_return) as mock_download, \
             patch.object(codex_oauth, "_cpa_management_origin", return_value="http://127.0.0.1:8317"), \
             patch.object(codex_oauth, "_cpa_management_key", return_value="test-key"), \
             patch.object(codex_oauth.curl_requests, "Session", return_value=fake_session), \
             patch.object(codex_oauth, "CurlMime", return_value=fake_mime):
            result = codex_oauth.upload_cpa_auth_file(email=email)
        return result, mock_get, mock_download, fake_session, fake_mime

    def test_prefers_account_token_and_skips_download(self):
        token = _make_jwt(_payload_with_account())
        account = {"email": "user@example.com", "access_token": token, "plan_type": "free"}
        result, mock_get, mock_download, fake_session, fake_mime = self._run_upload(account)

        self.assertEqual(result, {"status": "ok"})
        mock_get.assert_called_once_with("user@example.com")
        mock_download.assert_not_called()
        self.assertEqual(len(fake_session.calls), 1)
        file_part = next(p for p in fake_mime.parts if p["name"] == "file")
        self.assertEqual(file_part["filename"], "codex-user@example.com-free.json")
        payload = json.loads(file_part["data"].decode("utf-8"))
        self.assertEqual(payload["access_token"], token)
        self.assertEqual(payload["account_id"], "a03cd290-f321-4704-9984-3a05633d610c")
        self.assertEqual(payload["type"], "codex")

    def test_no_account_token_falls_back_to_download(self):
        download_content = '{"access_token": "sk-cpa-existing"}'
        result, mock_get, mock_download, fake_session, fake_mime = self._run_upload(
            {"email": "user@example.com", "access_token": ""},
            download_return=(download_content, "codex-user@example.com-free.json", {}),
        )

        self.assertEqual(result, {"status": "ok"})
        mock_get.assert_called_once_with("user@example.com")
        mock_download.assert_called_once_with(email="user@example.com")
        file_part = next(p for p in fake_mime.parts if p["name"] == "file")
        self.assertEqual(file_part["data"], b'{"access_token": "sk-cpa-existing"}')

    def test_account_lookup_error_falls_back_to_download(self):
        download_content = '{"access_token": "sk-cpa-existing"}'
        fake_session = _FakeSession(200, {"status": "ok"})
        fake_mime = _FakeMime()
        with patch.object(core_db, "get_account_by_email", side_effect=RuntimeError("db down")) as mock_get, \
             patch.object(codex_oauth, "download_cpa_codex_auth_text", return_value=(download_content, "codex-user@example.com-free.json", {})) as mock_download, \
             patch.object(codex_oauth, "_cpa_management_origin", return_value="http://127.0.0.1:8317"), \
             patch.object(codex_oauth, "_cpa_management_key", return_value="test-key"), \
             patch.object(codex_oauth.curl_requests, "Session", return_value=fake_session), \
             patch.object(codex_oauth, "CurlMime", return_value=fake_mime):
            result = codex_oauth.upload_cpa_auth_file(email="user@example.com")

        self.assertEqual(result, {"status": "ok"})
        mock_get.assert_called_once_with("user@example.com")
        mock_download.assert_called_once_with(email="user@example.com")

    def test_build_content_from_account_saves_local_credential(self):
        token = _make_jwt(_payload_with_account())
        account = {"email": "user@example.com", "access_token": token, "plan_type": "free"}
        with patch.object(core_db, "get_account_by_email", return_value=account), \
             tempfile.TemporaryDirectory() as tmp:
            with patch.object(codex_oauth, "_PROJECT_ROOT", Path(tmp)):
                content, name = codex_oauth._build_cpa_content_from_account("user@example.com")
            expected = Path(tmp) / "codex_accounts" / "codex-user@example.com-free.json"
            self.assertTrue(expected.exists())
            saved = json.loads(expected.read_text(encoding="utf-8"))
            self.assertEqual(saved["access_token"], token)
            self.assertEqual(saved["account_id"], "a03cd290-f321-4704-9984-3a05633d610c")
            self.assertEqual(name, "codex-user@example.com-free.json")
            self.assertIn("access_token", content)


class ProjectRootTests(unittest.TestCase):
    def test_project_root_points_to_turb_gpt_free_register(self):
        """_PROJECT_ROOT 应指向 turb-gpt-free-register/（core/ 的父目录）。"""
        self.assertEqual(
            codex_oauth._PROJECT_ROOT,
            Path(codex_oauth.__file__).resolve().parent.parent,
        )
        self.assertTrue(codex_oauth._PROJECT_ROOT.name == "turb-gpt-free-register")

    def test_save_codex_credential_writes_to_codex_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(codex_oauth, "_PROJECT_ROOT", Path(tmp)):
                path = codex_oauth.save_codex_credential(
                    {"type": "codex", "access_token": "sk-x"}, "user@example.com", "free"
                )
            self.assertEqual(path, Path(tmp) / "codex_accounts" / "codex-user@example.com-free.json")
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
