# -*- coding: utf-8 -*-
"""回归测试：测活网络预检中 proxy="" 的"直连"契约不被吞掉。

背景：`_network_preflight_with_retry` 曾写 `BrowserSession(proxy=proxy if proxy else None)`，
空串 "" 被转成 None，而 BrowserSession 中 None 语义是"从代理池随机抽"，
导致 live_check_service 的"直连兜底"实际仍走代理池（出口 IP 被 CF 封时兜底无效，一直 403）。
"""
import unittest
from unittest import mock

from core import account_liveness


class _FakeRawSession:
    """模拟 curl_cffi 底层会话，只支持 close()。"""

    def close(self) -> None:
        pass


class _FakeBrowserSession:
    """捕获构造参数（尤其 proxy）的 BrowserSession 替身。"""

    def __init__(self, proxy=None):
        self.proxy = proxy
        self.device_id = "did-test"
        self.session = _FakeRawSession()


class NetworkPreflightProxyContractTests(unittest.TestCase):
    """锁定 BrowserSession 构造时 proxy 参数原样透传的契约。"""

    def _run_preflight(self, proxy):
        with mock.patch.object(account_liveness, "BrowserSession", _FakeBrowserSession), \
             mock.patch.object(account_liveness, "get_providers", return_value={}), \
             mock.patch.object(account_liveness, "get_csrf_token", return_value="csrf-test"), \
             mock.patch.object(account_liveness, "signin_openai", return_value="https://auth.openai.com/authorize"):
            session, authorize_url = account_liveness._network_preflight_with_retry(
                email="user@example.com", proxy=proxy, max_attempts=1
            )
        return session, authorize_url

    def test_empty_string_proxy_passes_through_verbatim(self):
        """proxy="" 必须原样传给 BrowserSession（旧代码会吞成 None，此测试失败）。"""
        session, authorize_url = self._run_preflight(proxy="")
        self.assertIsInstance(session, _FakeBrowserSession)
        self.assertEqual(session.proxy, "")
        self.assertEqual(authorize_url, "https://auth.openai.com/authorize")

    def test_none_proxy_passes_through_verbatim(self):
        """proxy=None 保持默认语义（BrowserSession 内部从代理池随机抽）。"""
        session, _ = self._run_preflight(proxy=None)
        self.assertIsInstance(session, _FakeBrowserSession)
        self.assertIsNone(session.proxy)

    def test_explicit_proxy_passes_through_verbatim(self):
        """proxy='socks5://...' 保持指定代理语义。"""
        session, _ = self._run_preflight(proxy="socks5://127.0.0.1:7890")
        self.assertIsInstance(session, _FakeBrowserSession)
        self.assertEqual(session.proxy, "socks5://127.0.0.1:7890")


if __name__ == "__main__":
    unittest.main()
