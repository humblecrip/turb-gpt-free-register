# -*- coding: utf-8 -*-
"""单测：socks5:// → socks5h:// 代理规范化。

背景：curl_cffi 走 socks5://（无 h，DNS 本地解析）会报 curl (35) BoringSSL SSL_connect
握手失败；socks5h://（DNS 代理端解析）握手正常。normalize_proxy_scheme 兜底转换，
BrowserSession / pick_proxy 一律产出 socks5h://。
"""
import unittest
from unittest import mock

from config import proxy as proxy_module
from config.proxy import normalize_proxy_scheme
from core.session import BrowserSession


class NormalizeProxySchemeTests(unittest.TestCase):
    def test_socks5_converted_to_socks5h(self):
        self.assertEqual(
            normalize_proxy_scheme("socks5://127.0.0.1:7890"),
            "socks5h://127.0.0.1:7890",
        )

    def test_socks5_with_credentials_converted_to_socks5h(self):
        self.assertEqual(
            normalize_proxy_scheme("socks5://user:pass@127.0.0.1:7890"),
            "socks5h://user:pass@127.0.0.1:7890",
        )

    def test_socks5h_unchanged(self):
        self.assertEqual(
            normalize_proxy_scheme("socks5h://127.0.0.1:7890"),
            "socks5h://127.0.0.1:7890",
        )

    def test_http_unchanged(self):
        self.assertEqual(
            normalize_proxy_scheme("http://127.0.0.1:7890"),
            "http://127.0.0.1:7890",
        )

    def test_https_unchanged(self):
        self.assertEqual(
            normalize_proxy_scheme("https://127.0.0.1:7890"),
            "https://127.0.0.1:7890",
        )

    def test_none_unchanged(self):
        self.assertIsNone(normalize_proxy_scheme(None))

    def test_empty_string_unchanged(self):
        self.assertEqual(normalize_proxy_scheme(""), "")

    def test_uppercase_socks5_converted(self):
        self.assertEqual(
            normalize_proxy_scheme("SOCKS5://127.0.0.1:7890"),
            "socks5h://127.0.0.1:7890",
        )


class PickProxyNormalizeTests(unittest.TestCase):
    def test_pick_proxy_normalizes_socks5_entry(self):
        original_pool = proxy_module.PROXY_POOL
        try:
            proxy_module.PROXY_POOL = ["socks5://127.0.0.1:7890"]
            with mock.patch("config.proxy.random.choice", return_value="socks5://127.0.0.1:7890"):
                self.assertEqual(proxy_module.pick_proxy(), "socks5h://127.0.0.1:7890")
        finally:
            proxy_module.PROXY_POOL = original_pool

    def test_pick_proxy_keeps_socks5h_entry(self):
        original_pool = proxy_module.PROXY_POOL
        try:
            proxy_module.PROXY_POOL = ["socks5h://127.0.0.1:7890"]
            with mock.patch("config.proxy.random.choice", return_value="socks5h://127.0.0.1:7890"):
                self.assertEqual(proxy_module.pick_proxy(), "socks5h://127.0.0.1:7890")
        finally:
            proxy_module.PROXY_POOL = original_pool

    def test_pick_proxy_empty_pool_returns_empty_string(self):
        original_pool = proxy_module.PROXY_POOL
        try:
            proxy_module.PROXY_POOL = []
            self.assertEqual(proxy_module.pick_proxy(), "")
        finally:
            proxy_module.PROXY_POOL = original_pool


class _FakeCookieJar:
    def __init__(self):
        self.jar = []

    def set(self, *args, **kwargs):
        pass


class _FakeRawSession:
    """模拟 curl_cffi 底层会话，避免测试真实联网/依赖 curl_cffi 行为。"""

    def __init__(self):
        self.proxies = {}
        self.timeout = None
        self.cookies = _FakeCookieJar()


class BrowserSessionProxyNormalizeTests(unittest.TestCase):
    def _make_session(self, proxy):
        with mock.patch("core.session.Session") as fake_cls:
            fake_cls.return_value = _FakeRawSession()
            return BrowserSession(proxy=proxy, detect_exit_geo=False)

    def test_explicit_socks5_proxy_normalized_internal(self):
        session = self._make_session("socks5://127.0.0.1:7890")
        self.assertEqual(session.proxy, "socks5h://127.0.0.1:7890")

    def test_explicit_socks5h_proxy_unchanged(self):
        session = self._make_session("socks5h://127.0.0.1:7890")
        self.assertEqual(session.proxy, "socks5h://127.0.0.1:7890")

    def test_empty_string_proxy_keeps_direct_contract(self):
        session = self._make_session("")
        self.assertEqual(session.proxy, "")


if __name__ == "__main__":
    unittest.main()
