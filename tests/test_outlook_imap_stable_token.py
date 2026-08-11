# -*- coding: utf-8 -*-
"""_fetch_imap_direct_messages 稳定 token 源（entra_outlook）+ Junk 文件夹单元测试。

验证：
- IMAP 取件始终使用 _ms_access_token(preferred_kind="outlook") 拿 token
- 不再调用 _live_imap_access_token（live_imap 兜底已移除）
- XOAUTH2 使用该 token 认证；同时查询 INBOX + Junk，_fetch_source 标注 imap_inbox / imap_junk
- INBOX + Junk 合并去重（message-id 优先，缺失时用 subject+date）
- Junk 查询失败不影响 INBOX 结果
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


def _raw_message(subject="OTP mail", message_id=None):
    mid_header = f"Message-ID: <{message_id}>\r\n" if message_id else ""
    return email_lib.message_from_string(
        f"Subject: {subject}\r\n"
        f"{mid_header}"
        "From: no-reply@openai.com\r\n"
        "To: user@outlook.com\r\n"
        "Date: Tue, 11 Aug 2026 10:00:00 +0000\r\n"
        "\r\n"
        "Your code is 123456\r\n"
    ).as_bytes()


class FakeIMAP:
    """迷你 imaplib.IMAP4_SSL fake，记录 authenticate 回调，支持多文件夹取件。"""

    def __init__(self, host, port, messages=None, folders=None):
        self.host = host
        self.port = port
        self.messages = messages or []   # 未指定 folders 时的默认消息
        self.folders = folders or {}     # {folder_name: [raw messages]}
        self.selected = None
        self.auth_cb = None
        self.logged_out = False
        self.select_calls = []
        self.fail_select = set()         # 该文件夹 select 时抛异常

    def authenticate(self, mechanism, authobject):
        self.auth_cb = authobject

    def select(self, mailbox):
        self.select_calls.append(mailbox)
        if mailbox in self.fail_select:
            raise RuntimeError(f"select {mailbox} failed")
        self.selected = mailbox
        return ("OK", [b"0"])

    def search(self, charset, criterion):
        msgs = self.folders.get(self.selected, self.messages)
        ids = " ".join(str(i) for i in range(1, len(msgs) + 1))
        return ("OK", [ids.encode()])

    def fetch(self, mid, parts):
        msgs = self.folders.get(self.selected, self.messages)
        raw = msgs[int(mid) - 1]
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
        fake = FakeIMAP(
            "outlook.office365.com",
            993,
            folders={"INBOX": [_raw_message(), _raw_message("second")], "Junk": []},
        )

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

        # INBOX + Junk 都被查询；Junk 为空时只返回 INBOX 邮件并标记 imap_inbox
        self.assertEqual(fake.select_calls, ["INBOX", "Junk"])
        self.assertEqual(len(out), 2)
        self.assertTrue(all(item["_fetch_source"] == "imap_inbox" for item in out))
        self.assertTrue(fake.logged_out)

    def test_queries_inbox_and_junk_merges_dedup(self):
        account = _make_account()
        # INBOX: A/B；Junk: B（同一封）/C → 合并后 A/B/C，B 去重（INBOX 优先）
        msg_a = _raw_message("A mail")
        msg_b = _raw_message("B mail")
        msg_c = _raw_message("C mail")
        fake = FakeIMAP(
            "outlook.office365.com",
            993,
            folders={"INBOX": [msg_a, msg_b], "Junk": [msg_b, msg_c]},
        )

        with patch("core.outlook_client.imaplib.IMAP4_SSL", return_value=fake), patch(
            "core.outlook_client._ms_access_token", return_value=("entra-tok-abc", "outlook")
        ):
            out = _fetch_imap_direct_messages(account)

        self.assertEqual(fake.select_calls, ["INBOX", "Junk"])
        self.assertEqual(len(out), 3)
        subjects = sorted(item["subject"] for item in out)
        self.assertEqual(subjects, ["A mail", "B mail", "C mail"])
        # B 来自 INBOX（INBOX 先插入，Junk 副本被去重）
        b_items = [item for item in out if item["subject"] == "B mail"]
        self.assertEqual(len(b_items), 1)
        self.assertEqual(b_items[0]["_fetch_source"], "imap_inbox")
        c_items = [item for item in out if item["subject"] == "C mail"]
        self.assertEqual(c_items[0]["_fetch_source"], "imap_junk")

    def test_dedupe_by_message_id_preferred(self):
        account = _make_account()
        # 同一 Message-ID、不同 subject → 只保留一封（message-id 优先于 subject+date）
        msg_inbox = _raw_message("Subject A", message_id="same-123@outlook.com")
        msg_junk = _raw_message("Subject B", message_id="same-123@outlook.com")
        fake = FakeIMAP(
            "outlook.office365.com",
            993,
            folders={"INBOX": [msg_inbox], "Junk": [msg_junk]},
        )

        with patch("core.outlook_client.imaplib.IMAP4_SSL", return_value=fake), patch(
            "core.outlook_client._ms_access_token", return_value=("entra-tok-abc", "outlook")
        ):
            out = _fetch_imap_direct_messages(account)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["subject"], "Subject A")  # INBOX 优先
        self.assertEqual(out[0]["_fetch_source"], "imap_inbox")

    def test_junk_select_failure_does_not_block_inbox(self):
        account = _make_account()
        fake = FakeIMAP(
            "outlook.office365.com",
            993,
            folders={"INBOX": [_raw_message("A mail"), _raw_message("B mail")], "Junk": []},
        )
        fake.fail_select = {"Junk"}

        with patch("core.outlook_client.imaplib.IMAP4_SSL", return_value=fake), patch(
            "core.outlook_client._ms_access_token", return_value=("entra-tok-abc", "outlook")
        ), patch("core.outlook_client.logger") as mock_logger:
            out = _fetch_imap_direct_messages(account)

        # Junk select 抛异常 → 跳过，仍返回 INBOX 的邮件
        self.assertEqual(fake.select_calls, ["INBOX", "Junk"])
        self.assertEqual(len(out), 2)
        self.assertTrue(all(item["_fetch_source"] == "imap_inbox" for item in out))
        # Junk 失败有 warning 日志
        self.assertTrue(any("Junk" in str(c) for c in mock_logger.warning.call_args_list))
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
        "_fetch_source": "imap_inbox",
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
