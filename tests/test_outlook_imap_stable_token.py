# -*- coding: utf-8 -*-
"""_fetch_imap_direct_messages 稳定 token 源（entra_outlook）单元测试。

验证：
- IMAP 取件始终使用 _ms_access_token(preferred_kind="outlook") 拿 token
- 不再调用 _live_imap_access_token（live_imap 兜底已移除）
- XOAUTH2 使用该 token 认证；取到的邮件 _fetch_source == "imap_entra_outlook"
- 认证失败 / token 获取失败时不真连网络，按现有契约返回 [] 并记 warning
- _fetch_via / fetch_latest_otp 的 force_direct：强制本地直连、绕开远端 session
- icloud_hme_client 转发取码传 force_direct=True
"""
import email as email_lib
import unittest
from unittest.mock import patch

from core.outlook_client import (
    OutlookAccount,
    OutlookClientError,
    _fetch_imap_direct_messages,
)


def _make_account(**kwargs):
    defaults = dict(
        email="user@outlook.com",
        password="pw",
        client_id="client-id",
        refresh_token="refresh-token",
    )
    defaults.update(kwargs)
    return OutlookAccount(**defaults)


def _raw_message(subject="OTP mail"):
    return email_lib.message_from_string(
        f"Subject: {subject}\r\n"
        "From: no-reply@openai.com\r\n"
        "To: user@outlook.com\r\n"
        "Date: Tue, 11 Aug 2026 10:00:00 +0000\r\n"
        "\r\n"
        "Your code is 123456\r\n"
    ).as_bytes()


class FakeIMAP:
    """迷你 imaplib.IMAP4_SSL fake，记录 authenticate 回调，支持本地取件流程。"""

    def __init__(self, host, port, messages=None):
        self.host = host
        self.port = port
        self.messages = messages or []
        self.auth_cb = None
        self.logged_out = False

    def authenticate(self, mechanism, authobject):
        self.auth_cb = authobject

    def select(self, mailbox):
        return ("OK", [b"0"])

    def search(self, charset, criterion):
        ids = " ".join(str(i) for i in range(1, len(self.messages) + 1))
        return ("OK", [ids.encode()])

    def fetch(self, mid, parts):
        raw = self.messages[int(mid) - 1]
        return ("OK", [(mid, raw)])

    def logout(self):
        self.logged_out = True
        return ("BYE", [])


class FetchImapDirectStableTokenTests(unittest.TestCase):
    def setUp(self):
        from core import outlook_client

        outlook_client._MS_TOKEN_CACHE.clear()
        outlook_client._MS_TOKEN_FATAL_CACHE.clear()

    def test_uses_entra_outlook_token_and_xoauth2_no_live_imap(self):
        account = _make_account()
        fake = FakeIMAP("outlook.office365.com", 993, messages=[_raw_message(), _raw_message("second")])

        with patch("core.outlook_client.imaplib.IMAP4_SSL", return_value=fake) as imap_cls, patch(
            "core.outlook_client._ms_access_token", return_value=("entra-tok-abc", "outlook")
        ) as ms_token, patch("core.outlook_client._live_imap_access_token") as live_token:
            out = _fetch_imap_direct_messages(account)

        # token 来自 _ms_access_token(preferred_kind="outlook")，且 http 复用同一 session
        ms_token.assert_called_once()
        call_kwargs = ms_token.call_args
        self.assertEqual(call_kwargs.kwargs.get("preferred_kind"), "outlook")
        self.assertIsNotNone(call_kwargs.kwargs.get("http"))
        # live_imap 兜底不再被调用
        live_token.assert_not_called()

        # XOAUTH2 使用该 token 认证
        self.assertEqual(imap_cls.call_args.args, ("outlook.office365.com", 993))
        self.assertIsNotNone(fake.auth_cb)
        auth_string = fake.auth_cb(None).decode("utf-8")
        self.assertIn("user=user@outlook.com", auth_string)
        self.assertIn("auth=Bearer entra-tok-abc", auth_string)

        # 正常取件并标记稳定 token 源
        self.assertEqual(len(out), 2)
        self.assertTrue(all(item["_fetch_source"] == "imap_entra_outlook" for item in out))
        self.assertTrue(fake.logged_out)

    def test_empty_inbox_returns_empty(self):
        account = _make_account()
        fake = FakeIMAP("outlook.office365.com", 993, messages=[])

        with patch("core.outlook_client.imaplib.IMAP4_SSL", return_value=fake), patch(
            "core.outlook_client._ms_access_token", return_value=("entra-tok-abc", "outlook")
        ), patch("core.outlook_client._live_imap_access_token") as live_token:
            out = _fetch_imap_direct_messages(account)

        self.assertEqual(out, [])
        live_token.assert_not_called()

    def test_auth_failure_returns_empty_and_logs_warning(self):
        account = _make_account()
        fake = FakeIMAP("outlook.office365.com", 993)

        def _fail_auth(mechanism, authobject):
            raise RuntimeError("AUTHENTICATE failed")

        fake.authenticate = _fail_auth

        with patch("core.outlook_client.imaplib.IMAP4_SSL", return_value=fake), patch(
            "core.outlook_client._ms_access_token", return_value=("entra-tok-abc", "outlook")
        ), patch("core.outlook_client._live_imap_access_token") as live_token, patch(
            "core.outlook_client.logger"
        ) as mock_logger:
            out = _fetch_imap_direct_messages(account)

        self.assertEqual(out, [])
        live_token.assert_not_called()
        mock_logger.warning.assert_called()

    def test_ms_access_token_failure_returns_empty(self):
        account = _make_account()

        with patch(
            "core.outlook_client._ms_access_token",
            side_effect=OutlookClientError("Microsoft OAuth refresh_token 换 token 失败: invalid_grant"),
        ) as ms_token, patch("core.outlook_client._live_imap_access_token") as live_token:
            out = _fetch_imap_direct_messages(account)

        self.assertEqual(out, [])
        ms_token.assert_called_once()
        live_token.assert_not_called()


class _Clock:
    """单调递增假时钟，替代 time.time。"""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        self.t += 1.0
        return self.t


def _openai_msg(subject="Your OpenAI verification code is 123456"):
    return {
        "subject": subject,
        "date": "2026-08-11T10:00:00Z",
        "body": "Your code is 123456",
        "_fetch_source": "imap_entra_outlook",
    }


class FetchViaForceDirectTests(unittest.TestCase):
    def setUp(self):
        from core import outlook_client

        outlook_client._MS_TOKEN_CACHE.clear()
        outlook_client._MS_TOKEN_FATAL_CACHE.clear()
        outlook_client._REMOTE_DISABLED = False

    def test_force_direct_imap_skips_remote(self):
        from core import outlook_client

        account = _make_account()
        with patch("core.outlook_client._outlook_fetch_mode", return_value="auto"), patch(
            "core.outlook_client._fetch_via_graph_direct", return_value=[]
        ) as graph_direct, patch(
            "core.outlook_client._fetch_imap_direct_messages", return_value=[{"id": "m1"}]
        ) as imap_direct, patch("core.outlook_client._secure_post") as secure_post:
            out = outlook_client._fetch_via(None, "imap", account, force_direct=True)

        imap_direct.assert_called_once_with(account)
        graph_direct.assert_not_called()
        secure_post.assert_not_called()
        self.assertEqual(out, [{"id": "m1"}])

    def test_force_direct_graph_skips_remote(self):
        from core import outlook_client

        account = _make_account()
        with patch("core.outlook_client._outlook_fetch_mode", return_value="auto"), patch(
            "core.outlook_client._fetch_via_graph_direct", return_value=[{"id": "g1"}]
        ) as graph_direct, patch(
            "core.outlook_client._fetch_imap_direct_messages", return_value=[]
        ) as imap_direct, patch("core.outlook_client._secure_post") as secure_post:
            out = outlook_client._fetch_via(None, "graph", account, force_direct=True)

        graph_direct.assert_called_once_with(account)
        imap_direct.assert_not_called()
        secure_post.assert_not_called()
        self.assertEqual(out, [{"id": "g1"}])

    def test_force_direct_unknown_protocol_returns_empty(self):
        from core import outlook_client

        account = _make_account()
        with patch("core.outlook_client._outlook_fetch_mode", return_value="auto"), patch(
            "core.outlook_client._fetch_via_graph_direct"
        ) as graph_direct, patch(
            "core.outlook_client._fetch_imap_direct_messages"
        ) as imap_direct, patch("core.outlook_client._secure_post") as secure_post:
            out = outlook_client._fetch_via(None, "nonsense", account, force_direct=True)

        self.assertEqual(out, [])
        graph_direct.assert_not_called()
        imap_direct.assert_not_called()
        secure_post.assert_not_called()

    def test_no_force_direct_uses_remote_url(self):
        from core import outlook_client

        account = _make_account()
        remote_data = {"success": True, "emails": [{"id": "r1", "subject": "hi"}]}
        with patch("core.outlook_client._outlook_fetch_mode", return_value="remote"), patch(
            "core.outlook_client._fetch_via_graph_direct"
        ) as graph_direct, patch(
            "core.outlook_client._fetch_imap_direct_messages"
        ) as imap_direct, patch(
            "core.outlook_client._secure_post", return_value=remote_data
        ) as secure_post:
            out = outlook_client._fetch_via(object(), "imap", account)

        secure_post.assert_called_once()
        self.assertIn("/api/fetch-imap", secure_post.call_args.args[1])
        graph_direct.assert_not_called()
        imap_direct.assert_not_called()
        self.assertEqual(out, [{"id": "r1", "subject": "hi", "_fetch_source": "remote_imap"}])

    def test_no_force_direct_graph_uses_remote_url(self):
        from core import outlook_client

        account = _make_account()
        remote_data = {"success": True, "emails": [{"id": "r1", "subject": "hi"}]}
        with patch("core.outlook_client._outlook_fetch_mode", return_value="remote"), patch(
            "core.outlook_client._secure_post", return_value=remote_data
        ) as secure_post, patch("core.outlook_client._fetch_via_graph_direct") as graph_direct, patch(
            "core.outlook_client._fetch_imap_direct_messages"
        ) as imap_direct:
            out = outlook_client._fetch_via(object(), "graph", account)

        secure_post.assert_called_once()
        self.assertIn("/api/fetch-graph", secure_post.call_args.args[1])
        graph_direct.assert_not_called()
        imap_direct.assert_not_called()
        self.assertEqual(out, [{"id": "r1", "subject": "hi", "_fetch_source": "remote_graph"}])


class FetchLatestOtpForceDirectTests(unittest.TestCase):
    def setUp(self):
        from core import outlook_client

        outlook_client._MS_TOKEN_CACHE.clear()
        outlook_client._MS_TOKEN_FATAL_CACHE.clear()
        outlook_client._REMOTE_DISABLED = False

    def test_fetch_latest_otp_passes_force_direct(self):
        from core import outlook_client

        account = _make_account()
        with patch("core.outlook_client.get_account_context", return_value=account), patch(
            "core.outlook_client._http_session", return_value=object()
        ), patch("core.outlook_client._fetch_via", return_value=[_openai_msg()]) as fetch_via, patch(
            "core.outlook_client.time.sleep"
        ), patch("core.outlook_client.time.time", side_effect=_Clock()):
            otp = outlook_client.fetch_latest_otp(
                account.email, max_wait=10, poll_interval=1, settle_seconds=0, force_direct=True
            )

        self.assertEqual(otp, "123456")
        self.assertEqual(fetch_via.call_count, 2)  # graph + imap
        for call in fetch_via.call_args_list:
            self.assertIs(call.kwargs.get("force_direct"), True)

    def test_fetch_latest_otp_default_no_force_direct(self):
        from core import outlook_client

        account = _make_account()
        with patch("core.outlook_client.get_account_context", return_value=account), patch(
            "core.outlook_client._http_session", return_value=object()
        ), patch("core.outlook_client._fetch_via", return_value=[_openai_msg()]) as fetch_via, patch(
            "core.outlook_client.time.sleep"
        ), patch("core.outlook_client.time.time", side_effect=_Clock()):
            otp = outlook_client.fetch_latest_otp(
                account.email, max_wait=10, poll_interval=1, settle_seconds=0
            )

        self.assertEqual(otp, "123456")
        for call in fetch_via.call_args_list:
            self.assertIs(call.kwargs.get("force_direct"), False)


class ICloudForwardForceDirectTests(unittest.TestCase):
    def test_forward_target_passes_force_direct(self):
        from core import icloud_hme_client, outlook_client

        account = _make_account(email="target@outlook.com")
        with patch.object(
            icloud_hme_client, "_forward_target_email", return_value="target@outlook.com"
        ), patch.object(
            outlook_client, "get_account_context", return_value=account
        ), patch.object(
            outlook_client, "fetch_latest_otp", return_value="123456"
        ) as fake_fetch:
            otp = icloud_hme_client._fetch_otp_from_forward_target("alias@icloud.com", "acc-1", 0.0)

        self.assertEqual(otp, "123456")
        fake_fetch.assert_called_once()
        self.assertIs(fake_fetch.call_args.kwargs.get("force_direct"), True)


if __name__ == "__main__":
    unittest.main()
