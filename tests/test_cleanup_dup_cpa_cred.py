# -*- coding: utf-8 -*-
"""补跑上传后清理同邮箱旧假凭证的单元测试。

背景：oauth-callback 提交给 CPA 会自动落盘带 hash 前缀的假凭证
（codex-{hash}-{email}-free.json，status 空，未注册 runtime）；补跑成功后
upload_cpa_auth_file 上传规范凭证（codex-{email}-{plan}.json，active）。
两证并存导致 CPA 路由可能命中假凭证报 auth token not found。上传成功后应
自动清理同邮箱旧假凭证，且清理失败不阻塞上传成功。
"""
import unittest
from unittest.mock import patch

from core import codex_oauth
from core import cpa_reauth


class CleanupDuplicateCpaCredentialsTests(unittest.TestCase):
    EMAIL = "stumps.velour-0f@icloud.com"
    KEEP_NAME = "codex-stumps.velour-0f@icloud.com-free.json"
    FAKE_NAME = "codex-3cfac92e-stumps.velour-0f@icloud.com-free.json"

    def _run(self, files, *, email=None, keep_name=None):
        deleted = []
        with patch.object(codex_oauth, "list_cpa_codex_auth_files", return_value=files) as mock_list, \
             patch.object(
                 cpa_reauth, "delete_cpa_auth_file",
                 side_effect=lambda name: deleted.append(name),
             ) as mock_delete:
            count = codex_oauth._cleanup_duplicate_cpa_credentials(
                email or self.EMAIL,
                keep_name or self.KEEP_NAME,
            )
        return count, deleted, mock_list, mock_delete

    def test_deletes_same_email_fake_and_keeps_self(self):
        """同邮箱假凭证被删；本次上传的 name 不删；返回删除数量。"""
        files = [
            {"name": self.FAKE_NAME, "type": "codex", "email": self.EMAIL, "status": ""},
            {"name": self.KEEP_NAME, "type": "codex", "email": self.EMAIL, "status": "active"},
        ]
        count, deleted, mock_list, mock_delete = self._run(files)
        self.assertEqual(count, 1)
        self.assertEqual(deleted, [self.FAKE_NAME])
        mock_list.assert_called_once_with()
        mock_delete.assert_called_once_with(self.FAKE_NAME)

    def test_does_not_delete_active_credential(self):
        """同邮箱但 status=active 的其他凭证不删（防误删）。"""
        active_name = "codex-stumps.velour-0f@icloud.com-plus.json"
        files = [
            {"name": self.FAKE_NAME, "type": "codex", "email": self.EMAIL, "status": ""},
            {"name": active_name, "type": "codex", "email": self.EMAIL, "status": "active"},
            {"name": self.KEEP_NAME, "type": "codex", "email": self.EMAIL, "status": "active"},
        ]
        count, deleted, _, _ = self._run(files)
        self.assertEqual(count, 1)
        self.assertIn(self.FAKE_NAME, deleted)
        self.assertNotIn(active_name, deleted)

    def test_does_not_delete_other_email(self):
        """不同邮箱的凭证不删。"""
        other = {"name": "codex-other@example.com-free.json", "type": "codex", "email": "other@example.com", "status": ""}
        files = [other, {"name": self.KEEP_NAME, "type": "codex", "email": self.EMAIL, "status": "active"}]
        count, deleted, _, _ = self._run(files)
        self.assertEqual(count, 0)
        self.assertEqual(deleted, [])

    def test_does_not_delete_email_substring_of_longer_email_with_field(self):
        """目标邮箱是另一个更长邮箱 local part 的子串时，即使名字含子串也不删（email 字段已返回）。"""
        longer = {"name": "codex-anotheruser@example.com-free.json", "type": "codex", "email": "anotheruser@example.com", "status": ""}
        files = [longer, {"name": "codex-user@example.com-free.json", "type": "codex", "email": "user@example.com", "status": "active"}]
        count, deleted, _, _ = self._run(files, email="user@example.com", keep_name="codex-user@example.com-free.json")
        self.assertEqual(count, 0)
        self.assertEqual(deleted, [])

    def test_does_not_delete_email_substring_of_longer_email_without_field(self):
        """email 字段缺失时，名字里的子串匹配也必须是邮箱边界，不能误删更长邮箱的凭证。"""
        longer = {"name": "codex-anotheruser@example.com-free.json", "type": "codex", "email": "", "status": ""}
        files = [longer, {"name": "codex-user@example.com-free.json", "type": "codex", "email": "user@example.com", "status": "active"}]
        count, deleted, _, _ = self._run(files, email="user@example.com", keep_name="codex-user@example.com-free.json")
        self.assertEqual(count, 0)
        self.assertEqual(deleted, [])

    def test_deletes_hash_prefix_fake_without_email_field(self):
        """email 字段缺失、名字带 hash 前缀的假凭证（email 前是 '-' 分隔符）仍能匹配删除。"""
        fake = {"name": "codex-3cfac92e-stumps.velour-0f@icloud.com-free.json", "type": "codex", "email": "", "status": ""}
        files = [fake, {"name": self.KEEP_NAME, "type": "codex", "email": self.EMAIL, "status": "active"}]
        count, deleted, _, _ = self._run(files)
        self.assertEqual(count, 1)
        self.assertEqual(deleted, ["codex-3cfac92e-stumps.velour-0f@icloud.com-free.json"])

    def test_single_credential_deletes_nothing(self):
        """同邮箱只有 1 个凭证（本次上传的）时不删任何东西。"""
        files = [{"name": self.KEEP_NAME, "type": "codex", "email": self.EMAIL, "status": "active"}]
        count, deleted, _, mock_delete = self._run(files)
        self.assertEqual(count, 0)
        self.assertEqual(deleted, [])
        mock_delete.assert_not_called()

    def test_list_failure_returns_zero_without_raising(self):
        """拉取 auth-files 列表失败 → 返回 0，不抛出异常。"""
        with patch.object(codex_oauth, "list_cpa_codex_auth_files", side_effect=RuntimeError("CPA 不可达")):
            count = codex_oauth._cleanup_duplicate_cpa_credentials(self.EMAIL, self.KEEP_NAME)
        self.assertEqual(count, 0)

    def test_delete_failure_does_not_raise(self):
        """删除单个凭证失败 → 不抛出，返回已成功删除数。"""
        files = [
            {"name": self.FAKE_NAME, "type": "codex", "email": self.EMAIL, "status": ""},
            {"name": "codex-stumps.velour-0f@icloud.com-plus.json", "type": "codex", "email": self.EMAIL, "status": "error"},
            {"name": self.KEEP_NAME, "type": "codex", "email": self.EMAIL, "status": "active"},
        ]

        def _delete(name):
            if name == self.FAKE_NAME:
                raise RuntimeError("delete 失败")

        with patch.object(codex_oauth, "list_cpa_codex_auth_files", return_value=files), \
             patch.object(cpa_reauth, "delete_cpa_auth_file", side_effect=_delete):
            count = codex_oauth._cleanup_duplicate_cpa_credentials(self.EMAIL, self.KEEP_NAME)
        self.assertEqual(count, 1)

    def test_empty_email_or_keep_name_returns_zero(self):
        """缺 email 或 keep_name 时直接返回 0，不拉列表。"""
        with patch.object(codex_oauth, "list_cpa_codex_auth_files") as mock_list:
            self.assertEqual(codex_oauth._cleanup_duplicate_cpa_credentials("", self.KEEP_NAME), 0)
            self.assertEqual(codex_oauth._cleanup_duplicate_cpa_credentials(self.EMAIL, ""), 0)
            self.assertEqual(codex_oauth._cleanup_duplicate_cpa_credentials("not-an-email", self.KEEP_NAME), 0)
        mock_list.assert_not_called()


class EmailFromCpaNameTests(unittest.TestCase):
    def test_parses_canonical_name(self):
        self.assertEqual(
            codex_oauth._email_from_cpa_name("codex-user@example.com-free.json"),
            "user@example.com",
        )

    def test_parses_name_without_plan(self):
        self.assertEqual(
            codex_oauth._email_from_cpa_name("codex-user@example.com.json"),
            "user@example.com",
        )

    def test_parses_email_local_part_with_dash(self):
        self.assertEqual(
            codex_oauth._email_from_cpa_name("codex-foo-bar@example.com.json"),
            "foo-bar@example.com",
        )

    def test_returns_empty_for_unparsable(self):
        self.assertEqual(codex_oauth._email_from_cpa_name(""), "")
        self.assertEqual(codex_oauth._email_from_cpa_name("other-file.json"), "")
        self.assertEqual(codex_oauth._email_from_cpa_name("codex-no-at.json"), "")


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


class UploadCallsCleanupTests(unittest.TestCase):
    EMAIL = "user@example.com"
    NAME = "codex-user@example.com-free.json"

    def _run_upload(self, *, email=EMAIL, name="", content="", cleanup_side_effect=None):
        fake_session = _FakeSession(200, {"status": "ok"})
        fake_mime = _FakeMime()
        with patch.object(
            codex_oauth, "download_cpa_codex_auth_text",
            return_value=('{"access_token": "sk-x"}', self.NAME, {"name": self.NAME}),
        ), patch.object(
            codex_oauth, "_cpa_management_origin", return_value="http://127.0.0.1:8317"
        ), patch.object(codex_oauth, "_cpa_management_key", return_value="test-key"), patch.object(
            codex_oauth.curl_requests, "Session", return_value=fake_session
        ), patch.object(codex_oauth, "CurlMime", return_value=fake_mime), patch.object(
            codex_oauth, "_cleanup_duplicate_cpa_credentials",
            side_effect=cleanup_side_effect,
        ) as mock_cleanup:
            result = codex_oauth.upload_cpa_auth_file(email=email, name=name, content=content)
        return result, mock_cleanup

    def test_upload_success_calls_cleanup_with_email_and_name(self):
        """上传成功后调用清理，传入 email 与本次上传的 name。"""
        result, mock_cleanup = self._run_upload(email=self.EMAIL)
        self.assertEqual(result, {"status": "ok"})
        mock_cleanup.assert_called_once_with(self.EMAIL, self.NAME)

    def test_upload_cleanup_failure_does_not_block_success(self):
        """清理抛异常 → 仅告警，上传仍返回成功。"""
        result, mock_cleanup = self._run_upload(
            email=self.EMAIL,
            cleanup_side_effect=RuntimeError("清理挂了"),
        )
        self.assertEqual(result, {"status": "ok"})
        mock_cleanup.assert_called_once_with(self.EMAIL, self.NAME)

    def test_upload_with_name_content_derives_email_from_name(self):
        """显式 name+content 且不带 email 时，清理用从 name 解析出的邮箱。"""
        result, mock_cleanup = self._run_upload(email="", name=self.NAME, content='{"access_token": "sk-x"}')
        self.assertEqual(result, {"status": "ok"})
        mock_cleanup.assert_called_once_with("user@example.com", self.NAME)


if __name__ == "__main__":
    unittest.main()
