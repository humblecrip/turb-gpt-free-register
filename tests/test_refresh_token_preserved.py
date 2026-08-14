# -*- coding: utf-8 -*-
"""refresh_token 全链路保留的单元测试。

背景：补跑成功后上传 CPA 的凭证缺 refresh_token → access_token 过期后无法刷新
→ CPA 报 token has been invalidated (401)。修复要求：
  1) 账号库 db 补存 refresh_token（insert_account / update_account_tokens / 读取）
  2) build_credential_from_account 用账号库 refresh_token（不再强制置空）
  3) _build_cpa_content_from_account 优先本地落盘凭证文件（含 refresh_token），
     回退账号库构造
  4) run_codex_oauth 成功把 refresh_token 写回账号库
  5) CPA 模式授权成功后回填本地凭证 + 账号库
"""
import base64
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import codex_oauth, db as core_db


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


@contextmanager
def _db_paths(root: Path):
    """把 db 文件路径常量指向临时目录，避免测试污染真实数据文件。"""
    with ExitStack() as stack:
        for attr, fname in [
            ("_ACCOUNTS_JSON", "accounts.json"),
            ("_LEGACY_ACCOUNTS_JSON", "legacy_accounts.json"),
            ("_ACCOUNTS_TXT", "accounts.txt"),
            ("_TOKENS_TXT", "tokens.txt"),
            ("_VIEWER_HTML", "viewer.html"),
            ("_OUTLOOK_JSON", "outlook.json"),
            ("_LEGACY_OUTLOOK_JSON", "legacy_outlook.json"),
            ("_OUTLOOK_TXT", "outlook.txt"),
        ]:
            stack.enter_context(patch.object(core_db, attr, root / fname))
        yield


@contextmanager
def _patchers(patch_list):
    """把多个 patch 组合成一个上下文管理器。"""
    with ExitStack() as stack:
        for p in patch_list:
            stack.enter_context(p)
        yield


class AccountDbRefreshTokenTests(unittest.TestCase):
    def test_insert_account_stores_and_reads_refresh_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with _db_paths(root):
                row_id = core_db.insert_account(
                    email="user@example.com",
                    access_token="sk-1",
                    refresh_token="rt-1",
                    plan_type="free",
                )
                self.assertTrue(row_id)
                account = core_db.get_account_by_email("user@example.com")
                self.assertEqual(account["refresh_token"], "rt-1")
                self.assertEqual(account["access_token"], "sk-1")
                self.assertEqual(account["plan_type"], "free")

    def test_old_account_without_refresh_token_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with _db_paths(root):
                core_db.insert_account(email="user@example.com", access_token="sk-1")
                (root / "accounts.json").write_text(
                    json.dumps(json.loads((root / "accounts.json").read_text(encoding="utf-8"))),
                    encoding="utf-8",
                )
                account = core_db.get_account_by_email("user@example.com")
                self.assertEqual(account.get("refresh_token", "missing"), "")
                self.assertEqual(account["refresh_token"], "")

    def test_update_account_tokens_writes_refresh_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with _db_paths(root):
                core_db.insert_account(email="user@example.com", access_token="sk-old")
                self.assertTrue(core_db.update_account_tokens(
                    "user@example.com",
                    access_token="sk-new",
                    refresh_token="rt-new",
                    plan_type="plus",
                ))
                account = core_db.get_account_by_email("user@example.com")
                self.assertEqual(account["refresh_token"], "rt-new")
                self.assertEqual(account["access_token"], "sk-new")
                self.assertEqual(account["plan_type"], "plus")

    def test_update_account_tokens_missing_account_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with _db_paths(root):
                self.assertFalse(core_db.update_account_tokens("ghost@example.com", refresh_token="rt-x"))

    def test_update_account_tokens_keeps_untouched_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with _db_paths(root):
                core_db.insert_account(email="user@example.com", access_token="sk-1", refresh_token="rt-1")
                self.assertTrue(core_db.update_account_tokens("user@example.com", refresh_token="rt-2"))
                account = core_db.get_account_by_email("user@example.com")
                self.assertEqual(account["refresh_token"], "rt-2")
                self.assertEqual(account["access_token"], "sk-1")


class BuildCredentialFromAccountRefreshTokenTests(unittest.TestCase):
    def test_preserves_passed_refresh_token(self):
        token = _make_jwt(_payload_with_account())
        cred = codex_oauth.build_credential_from_account("user@example.com", token, "rt-196chars")
        self.assertEqual(cred["refresh_token"], "rt-196chars")
        self.assertEqual(cred["access_token"], token)

    def test_default_refresh_token_empty_for_backward_compat(self):
        token = _make_jwt(_payload_with_account())
        cred = codex_oauth.build_credential_from_account("user@example.com", token)
        self.assertEqual(cred["refresh_token"], "")


class LoadLocalCredentialTests(unittest.TestCase):
    def test_reads_local_file_with_refresh_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_dir = root / "codex_accounts"
            out_dir.mkdir()
            (out_dir / "codex-user@example.com-free.json").write_text(
                json.dumps({"access_token": "sk-local", "refresh_token": "rt-local"}),
                encoding="utf-8",
            )
            with patch.object(codex_oauth, "_PROJECT_ROOT", root):
                result = codex_oauth._load_local_credential("user@example.com", "free")
            self.assertIsNotNone(result)
            content, name = result
            self.assertEqual(name, "codex-user@example.com-free.json")
            self.assertIn("sk-local", content)
            self.assertIn("rt-local", content)

    def test_skips_local_file_without_refresh_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_dir = root / "codex_accounts"
            out_dir.mkdir()
            (out_dir / "codex-user@example.com-free.json").write_text(
                json.dumps({"access_token": "sk-local"}),
                encoding="utf-8",
            )
            with patch.object(codex_oauth, "_PROJECT_ROOT", root):
                result = codex_oauth._load_local_credential("user@example.com", "free")
            self.assertIsNone(result)

    def test_skips_local_file_without_access_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_dir = root / "codex_accounts"
            out_dir.mkdir()
            (out_dir / "codex-user@example.com-free.json").write_text(
                json.dumps({"refresh_token": "rt-only"}),
                encoding="utf-8",
            )
            with patch.object(codex_oauth, "_PROJECT_ROOT", root):
                result = codex_oauth._load_local_credential("user@example.com", "free")
            self.assertIsNone(result)

    def test_returns_none_when_no_local_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(codex_oauth, "_PROJECT_ROOT", root):
                result = codex_oauth._load_local_credential("user@example.com", "free")
            self.assertIsNone(result)


class BuildCpaContentPrefersLocalFileTests(unittest.TestCase):
    def test_prefers_local_credential_file_over_account_db(self):
        account = {"email": "user@example.com", "access_token": "sk-db", "plan_type": "free"}
        with patch.object(core_db, "get_account_by_email", return_value=account) as mock_get, \
             patch.object(codex_oauth, "_load_local_credential",
                          return_value=('{"access_token": "sk-local", "refresh_token": "rt-local"}\n',
                                        "codex-user@example.com-free.json")):
            content, name = codex_oauth._build_cpa_content_from_account("user@example.com")
        self.assertEqual(name, "codex-user@example.com-free.json")
        self.assertIn("sk-local", content)
        self.assertIn("rt-local", content)
        mock_get.assert_called_once_with("user@example.com")

    def test_fallback_to_account_db_uses_account_refresh_token(self):
        token = _make_jwt(_payload_with_account())
        account = {"email": "user@example.com", "access_token": token, "plan_type": "free", "refresh_token": "rt-db"}
        with patch.object(core_db, "get_account_by_email", return_value=account), \
             patch.object(codex_oauth, "_load_local_credential", return_value=None), \
             patch.object(codex_oauth, "save_codex_credential",
                          return_value=Path("/tmp/codex-user@example.com-free.json")) as mock_save:
            content, name = codex_oauth._build_cpa_content_from_account("user@example.com")
        self.assertEqual(name, "codex-user@example.com-free.json")
        payload = json.loads(content)
        self.assertEqual(payload["access_token"], token)
        self.assertEqual(payload["refresh_token"], "rt-db")
        mock_save.assert_called_once()

    def test_no_account_token_returns_none(self):
        with patch.object(core_db, "get_account_by_email", return_value={"email": "user@example.com", "access_token": ""}), \
             patch.object(codex_oauth, "_load_local_credential", return_value=None):
            result = codex_oauth._build_cpa_content_from_account("user@example.com")
        self.assertEqual(result, (None, None))


class BackfillCpaCredentialTests(unittest.TestCase):
    def test_downloads_saves_local_and_writes_account_db(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            download_content = '{"access_token": "sk-cpa", "refresh_token": "rt-cpa", "type": "codex"}\n'
            with patch.object(codex_oauth, "download_cpa_codex_auth_text",
                              return_value=(download_content, "codex-user@example.com-free.json", {"name": "codex-user@example.com-free.json"})), \
                 patch.object(core_db, "get_account_by_email",
                              return_value={"email": "user@example.com", "plan_type": "free"}), \
                 patch.object(core_db, "update_account_tokens", return_value=True) as mock_update, \
                 patch.object(codex_oauth, "_PROJECT_ROOT", root):
                path = codex_oauth._backfill_cpa_credential_to_local("user@example.com")
            expected = root / "codex_accounts" / "codex-user@example.com-free.json"
            self.assertEqual(path, expected)
            self.assertTrue(expected.exists())
            saved = json.loads(expected.read_text(encoding="utf-8"))
            self.assertEqual(saved["access_token"], "sk-cpa")
            self.assertEqual(saved["refresh_token"], "rt-cpa")
            mock_update.assert_called_once_with(
                "user@example.com",
                access_token="sk-cpa",
                refresh_token="rt-cpa",
                plan_type="free",
            )

    def test_raises_when_download_missing_access_token(self):
        with patch.object(codex_oauth, "download_cpa_codex_auth_text",
                          return_value=('{"refresh_token": "rt-cpa"}\n', "codex-user@example.com-free.json", {})), \
             patch.object(core_db, "get_account_by_email", return_value=None):
            with self.assertRaises(RuntimeError):
                codex_oauth._backfill_cpa_credential_to_local("user@example.com")

    def test_raises_when_download_missing_refresh_token(self):
        # 缺 refresh_token 的凭证回填无意义：必须失败且不写任何内容（不覆盖账号库原值），
        # 否则上传的凭证依旧缺 rt，401 复现。
        with patch.object(codex_oauth, "download_cpa_codex_auth_text",
                          return_value=('{"access_token": "sk-cpa"}\n', "codex-user@example.com-free.json", {})), \
             patch.object(core_db, "get_account_by_email",
                          return_value={"email": "user@example.com", "plan_type": "free"}), \
             patch.object(core_db, "update_account_tokens", return_value=True) as mock_update, \
             patch.object(codex_oauth, "_PROJECT_ROOT", Path(tempfile.mkdtemp())):
            with self.assertRaises(RuntimeError):
                codex_oauth._backfill_cpa_credential_to_local("user@example.com")
        mock_update.assert_not_called()


class RunCodexOauthWritesRefreshTokenTests(unittest.TestCase):
    def _patch_local_flow(self, *, token_resp, id_claims):
        return _patchers([
            patch.object(codex_oauth._cfg, "CODEX_OAUTH_DRIVER", "protocol"),
            patch.object(codex_oauth, "_codex_auth_url_source", return_value="local"),
            patch.object(codex_oauth, "_generate_pkce", return_value=("verifier", "challenge")),
            patch.object(codex_oauth, "_generate_state", return_value="state-123"),
            patch.object(codex_oauth, "network_preflight", return_value=None),
            patch.object(codex_oauth, "human_delay", return_value=None),
            patch.object(codex_oauth, "BrowserSession", return_value=MagicMock()),
            patch.object(codex_oauth, "_bootstrap_authorize", return_value=None),
            patch.object(codex_oauth, "_submit_email", return_value=None),
            patch.object(codex_oauth, "_submit_email_otp", return_value=None),
            patch.object(codex_oauth, "_do_phone_verification", return_value=None),
            patch.object(codex_oauth, "_select_workspace_and_get_callback",
                         return_value="http://localhost:1455/auth/callback?code=ac_test"),
            patch.object(codex_oauth, "_extract_code", return_value="ac_test"),
            patch.object(codex_oauth, "exchange_codex_token", return_value=token_resp),
            patch.object(codex_oauth, "_parse_id_token", return_value=id_claims),
            patch.object(codex_oauth, "save_codex_credential",
                         return_value=Path("/tmp/codex-user@example.com-free.json")),
        ])

    def test_local_mode_success_writes_refresh_token_to_account_db(self):
        token_resp = {
            "access_token": "sk-new",
            "refresh_token": "rt-new",
            "id_token": "id-token",
            "expires_in": 3600,
        }
        id_claims = {"email": "user@example.com", "account_id": "acc-1", "plan_type": "free"}
        with patch.object(core_db, "update_account_tokens", return_value=True) as mock_update:
            with self._patch_local_flow(token_resp=token_resp, id_claims=id_claims):
                result = codex_oauth.run_codex_oauth(
                    "user@example.com",
                    force=True,
                    otp_provider=lambda email, after_ts=None: "123456",
                )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("status"), "success")
        mock_update.assert_called_once_with(
            "user@example.com",
            access_token="sk-new",
            refresh_token="rt-new",
            plan_type="free",
        )

    def test_cpa_mode_success_backfills_local_credential(self):
        cpa_auth = {"auth_url": "https://auth.openai.com/authorize?state=state-1", "state": "state-1"}
        with patch.object(codex_oauth._cfg, "CODEX_OAUTH_DRIVER", "protocol"), \
             patch.object(codex_oauth, "_codex_auth_url_source", return_value="cpa"), \
             patch.object(codex_oauth, "_request_cpa_authorize_url", return_value=cpa_auth), \
             patch.object(codex_oauth, "network_preflight", return_value=None), \
             patch.object(codex_oauth, "human_delay", return_value=None), \
             patch.object(codex_oauth, "BrowserSession", return_value=MagicMock()), \
             patch.object(codex_oauth, "_bootstrap_authorize", return_value=None), \
             patch.object(codex_oauth, "_submit_email", return_value=None), \
             patch.object(codex_oauth, "_submit_email_otp", return_value=None), \
             patch.object(codex_oauth, "_do_phone_verification", return_value=None), \
             patch.object(codex_oauth, "_select_workspace_and_get_callback",
                          return_value="http://localhost:1455/auth/callback?code=ac_test"), \
             patch.object(codex_oauth, "_extract_code", return_value="ac_test"), \
             patch.object(codex_oauth, "_submit_cpa_callback", return_value={"status": "ok"}), \
             patch.object(codex_oauth, "_save_cpa_local_record", return_value=None), \
             patch.object(codex_oauth, "_verify_cpa_auth_landed", return_value=True), \
             patch.object(codex_oauth, "_backfill_cpa_credential_to_local", return_value=None) as mock_backfill:
            result = codex_oauth.run_codex_oauth(
                "user@example.com",
                force=True,
                otp_provider=lambda email, after_ts=None: "123456",
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("status"), "success")
        mock_backfill.assert_called_once_with("user@example.com")


if __name__ == "__main__":
    unittest.main()
