# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import gptmail_client


class DomainPoolPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pool_file = Path(self._tmpdir.name) / "gptmail_good_domains.json"
        self._orig_pool_file = gptmail_client._DOMAIN_POOL_FILE
        gptmail_client._DOMAIN_POOL_FILE = self._pool_file
        gptmail_client._CONTEXT_CACHE.clear()

    def tearDown(self):
        gptmail_client._DOMAIN_POOL_FILE = self._orig_pool_file
        gptmail_client._CONTEXT_CACHE.clear()
        self._tmpdir.cleanup()

    def test_load_returns_empty_when_file_missing(self):
        self.assertEqual(gptmail_client._load_domain_pool(), {})

    def test_load_returns_empty_on_corrupted_json(self):
        self._pool_file.write_text("{not-json", encoding="utf-8")
        self.assertEqual(gptmail_client._load_domain_pool(), {})

    def test_load_returns_empty_on_non_dict_json(self):
        self._pool_file.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(gptmail_client._load_domain_pool(), {})

    def test_save_and_load_round_trip(self):
        gptmail_client._save_domain_pool({"5y.loseyourip.com": {"score": 3, "ok": 3, "fail": 0, "updated_at": "2026-08-12T10:00:00"}})
        loaded = gptmail_client._load_domain_pool()
        self.assertEqual(loaded["5y.loseyourip.com"]["score"], 3)

    def test_record_result_success_accumulates_score(self):
        gptmail_client.record_register_result("abc@5y.loseyourip.com", True)
        gptmail_client.record_register_result("def@5y.loseyourip.com", True)
        pool = json.loads(self._pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool["5y.loseyourip.com"]["score"], 2)
        self.assertEqual(pool["5y.loseyourip.com"]["ok"], 2)
        self.assertEqual(pool["5y.loseyourip.com"]["fail"], 0)
        self.assertIn("updated_at", pool["5y.loseyourip.com"])

    def test_record_result_failure_accumulates_fail(self):
        gptmail_client.record_register_result("abc@5y.loseyourip.com", False)
        pool = json.loads(self._pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool["5y.loseyourip.com"]["score"], -1)
        self.assertEqual(pool["5y.loseyourip.com"]["ok"], 0)
        self.assertEqual(pool["5y.loseyourip.com"]["fail"], 1)

    def test_record_result_mixed_domains_isolated(self):
        gptmail_client.record_register_result("a@one.test", True)
        gptmail_client.record_register_result("b@two.test", False)
        pool = gptmail_client._load_domain_pool()
        self.assertEqual(pool["one.test"]["score"], 1)
        self.assertEqual(pool["two.test"]["score"], -1)

    def test_record_result_ignores_invalid_email(self):
        gptmail_client.record_register_result("", True)
        gptmail_client.record_register_result(None, False)
        gptmail_client.record_register_result("no-at-sign", True)
        self.assertFalse(self._pool_file.exists())

    def test_record_result_resets_corrupted_entry(self):
        gptmail_client._save_domain_pool({"bad.test": {"score": "oops", "ok": 1}})
        gptmail_client.record_register_result("a@bad.test", True)
        pool = gptmail_client._load_domain_pool()
        self.assertEqual(pool["bad.test"]["score"], 1)
        self.assertEqual(pool["bad.test"]["ok"], 1)
        self.assertEqual(pool["bad.test"]["fail"], 0)

    def test_pick_pool_domain_returns_none_when_no_positive_score(self):
        gptmail_client._save_domain_pool({"a.test": {"score": 0}, "b.test": {"score": -2}})
        self.assertIsNone(gptmail_client._pick_pool_domain())

    def test_pick_pool_domain_selects_positive_score(self):
        gptmail_client._save_domain_pool({
            "low.test": {"score": -1},
            "high.test": {"score": 9},
            "neg.test": {"score": -5},
        })
        self.assertEqual(gptmail_client._pick_pool_domain(), "high.test")

    def test_pick_pool_domain_limits_candidates_to_top_n(self):
        gptmail_client._save_domain_pool({
            "a.test": {"score": 1},
            "b.test": {"score": 2},
            "c.test": {"score": 3},
            "d.test": {"score": 4},
        })
        with patch.object(gptmail_client.random, "choice", return_value=(4, "d.test")) as choice:
            self.assertEqual(gptmail_client._pick_pool_domain(), "d.test")
        chosen = choice.call_args.args[0]
        self.assertEqual([d for _, d in chosen], ["d.test", "c.test", "b.test"])


class PickAccountDomainTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pool_file = Path(self._tmpdir.name) / "gptmail_good_domains.json"
        self._orig_pool_file = gptmail_client._DOMAIN_POOL_FILE
        gptmail_client._DOMAIN_POOL_FILE = self._pool_file
        gptmail_client._CONTEXT_CACHE.clear()
        self._api_key = patch.object(gptmail_client._email_cfg, "GPTMAIL_API_KEY", "key-123", create=True)
        self._api_key.start()

    def tearDown(self):
        self._api_key.stop()
        gptmail_client._DOMAIN_POOL_FILE = self._orig_pool_file
        gptmail_client._CONTEXT_CACHE.clear()
        self._tmpdir.cleanup()

    def _ok_response(self, email):
        response = Mock(status_code=200)
        response.json.return_value = {"success": True, "data": {"email": email}}
        return response

    @patch("core.gptmail_client.requests.post")
    def test_pick_account_with_explicit_domain_posts_domain(self, post):
        post.return_value = self._ok_response("user@5y.loseyourip.com")
        account = gptmail_client.pick_account(domain="5y.loseyourip.com")
        self.assertEqual(account.email, "user@5y.loseyourip.com")
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"], {"domain": "5y.loseyourip.com"})

    @patch("core.gptmail_client.requests.post")
    @patch("core.gptmail_client.requests.get")
    def test_pick_account_prefers_pool_domain_in_pool_branch(self, get, post):
        gptmail_client._save_domain_pool({"good.test": {"score": 5, "ok": 5, "fail": 0, "updated_at": "x"}})
        post.return_value = self._ok_response("user@good.test")
        get.return_value = self._ok_response("user@random.test")
        with patch.object(gptmail_client.random, "random", return_value=0.9):
            account = gptmail_client.pick_account()
        self.assertEqual(account.email, "user@good.test")
        post.assert_called_once()
        get.assert_not_called()

    @patch("core.gptmail_client.requests.post")
    @patch("core.gptmail_client.requests.get")
    def test_pick_account_explores_random_in_explore_branch(self, get, post):
        gptmail_client._save_domain_pool({"good.test": {"score": 5, "ok": 5, "fail": 0, "updated_at": "x"}})
        get.return_value = self._ok_response("user@random.test")
        with patch.object(gptmail_client.random, "random", return_value=0.0):
            account = gptmail_client.pick_account()
        self.assertEqual(account.email, "user@random.test")
        get.assert_called_once()
        post.assert_not_called()

    @patch("core.gptmail_client.requests.post")
    @patch("core.gptmail_client.requests.get")
    def test_pick_account_falls_back_random_when_pool_empty(self, get, post):
        get.return_value = self._ok_response("user@random.test")
        with patch.object(gptmail_client.random, "random", return_value=0.9):
            account = gptmail_client.pick_account()
        self.assertEqual(account.email, "user@random.test")
        get.assert_called_once()
        post.assert_not_called()

    @patch("core.gptmail_client.requests.post")
    @patch("core.gptmail_client.requests.get")
    def test_pick_account_falls_back_random_when_pool_domain_fails(self, get, post):
        gptmail_client._save_domain_pool({"good.test": {"score": 5, "ok": 5, "fail": 0, "updated_at": "x"}})
        post.return_value = Mock(status_code=400)
        post.return_value.json.return_value = {"success": False, "error": "bad domain"}
        get.return_value = self._ok_response("user@random.test")
        with patch.object(gptmail_client.random, "random", return_value=0.9):
            account = gptmail_client.pick_account()
        self.assertEqual(account.email, "user@random.test")
        post.assert_called_once()
        get.assert_called_once()

    @patch("core.gptmail_client.requests.post")
    @patch("core.gptmail_client.requests.get")
    def test_pick_account_random_generates_with_get(self, get, post):
        get.return_value = self._ok_response("fresh@gptmail.test")
        account = gptmail_client.pick_account()
        self.assertEqual(account.email, "fresh@gptmail.test")
        get.assert_called_once()
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
