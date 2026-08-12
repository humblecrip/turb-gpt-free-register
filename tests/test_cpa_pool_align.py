# -*- coding: utf-8 -*-
"""账号池 vs CPA 对齐（core.cpa_pool_align）单元测试。

覆盖：
    - 匹配逻辑：按邮箱（小写）精确匹配、文件名兜底、列表缺 email 字段时从 name 解析
    - 有效性判定：元数据失效（disabled/status/error/unavailable/高失败）、
      元数据 active 的号实际 401 探测（401 → 失效，非 401 → 有效）
    - 汇总统计：summary 六项计数
    - cpa_only：CPA 有但账号池无的号单独列出

全部 mock 掉真实依赖（db.list_accounts / codex_oauth.list_cpa_codex_auth_files /
cpa_reauth._is_http_401），不碰网络。
"""
import unittest
from unittest.mock import patch

from core import cpa_pool_align as align


class CpaPoolAlignMatchTests(unittest.TestCase):
    def _run(self, pool, files, **kw):
        with patch("core.db.list_accounts", return_value=pool), \
             patch("core.codex_oauth.list_cpa_codex_auth_files", return_value=files), \
             patch("core.cpa_reauth._is_http_401", return_value=False):
            return align.align_account_pool_vs_cpa(**kw)

    def test_matches_by_email_and_counts_summary(self):
        pool = [
            {"email": "a@x.com", "codex_status": "success"},
            {"email": "B@X.com", "codex_status": "success"},
            {"email": "c@x.com", "codex_status": "failed"},
        ]
        files = [
            {"name": "codex-a@x.com-free.json", "email": "a@x.com", "status": "active", "success": 3, "failed": 0},
            {"name": "codex-b@x.com-free.json", "email": "b@x.com", "status": "active", "success": 2, "failed": 0},
            {"name": "codex-orphan@x.com-free.json", "email": "orphan@x.com", "status": "error"},
        ]
        result = self._run(pool, files)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["accounts"]), 3)
        # 邮箱匹配不区分大小写
        self.assertTrue(result["accounts"][0]["in_cpa"])
        self.assertTrue(result["accounts"][0]["cpa_valid"])
        self.assertTrue(result["accounts"][1]["in_cpa"])
        self.assertFalse(result["accounts"][2]["in_cpa"])
        self.assertEqual(result["accounts"][2]["note"], "CPA auth-files 中没有该邮箱的 codex 凭证")
        self.assertEqual(result["accounts"][0]["cpa_name"], "codex-a@x.com-free.json")
        # cpa_only 单独列出
        self.assertEqual(len(result["cpa_only"]), 1)
        self.assertEqual(result["cpa_only"][0]["email"], "orphan@x.com")
        # 汇总统计
        s = result["summary"]
        self.assertEqual(s["pool_total"], 3)
        self.assertEqual(s["in_cpa"], 2)
        self.assertEqual(s["cpa_valid"], 2)
        self.assertEqual(s["cpa_dead"], 0)
        self.assertEqual(s["not_in_cpa"], 1)
        self.assertEqual(s["cpa_only"], 1)

    def test_matches_by_email_in_name_when_field_missing(self):
        pool = [{"email": "a@x.com", "codex_status": "success"}]
        files = [{"name": "codex-a@x.com-free.json", "status": "active", "success": 1, "failed": 0}]
        result = self._run(pool, files)
        self.assertTrue(result["accounts"][0]["in_cpa"])
        self.assertTrue(result["accounts"][0]["cpa_valid"])

    def test_cpa_only_email_parsed_from_name(self):
        pool = [{"email": "a@x.com", "codex_status": "success"}]
        files = [{"name": "codex-ghost@x.com-free.json", "status": "disabled"}]
        result = self._run(pool, files)
        self.assertEqual(len(result["cpa_only"]), 1)
        self.assertEqual(result["cpa_only"][0]["email"], "ghost@x.com")
        self.assertEqual(result["cpa_only"][0]["name"], "codex-ghost@x.com-free.json")

    def test_pool_order_preserved_and_empty_pool(self):
        pool = [
            {"email": "z@x.com", "codex_status": ""},
            {"email": "a@x.com", "codex_status": ""},
        ]
        files = [{"name": "codex-a@x.com-free.json", "email": "a@x.com", "status": "active", "success": 1, "failed": 0}]
        result = self._run(pool, files)
        self.assertEqual([a["email"] for a in result["accounts"]], ["z@x.com", "a@x.com"])
        empty = self._run([], files)
        self.assertEqual(empty["summary"]["pool_total"], 0)
        self.assertEqual(empty["summary"]["not_in_cpa"], 0)
        self.assertEqual(len(empty["cpa_only"]), 1)


class CpaPoolAlignValidityTests(unittest.TestCase):
    def _files(self, **over):
        item = {
            "name": "codex-a@x.com-free.json",
            "email": "a@x.com",
            "status": "active",
            "disabled": False,
            "unavailable": False,
            "success": 3,
            "failed": 0,
        }
        item.update(over)
        return [item]

    def _run(self, files, probe_result=False, **kw):
        with patch("core.db.list_accounts", return_value=[{"email": "a@x.com", "codex_status": "success"}]), \
             patch("core.codex_oauth.list_cpa_codex_auth_files", return_value=files), \
             patch("core.cpa_reauth._is_http_401", return_value=probe_result):
            return align.align_account_pool_vs_cpa(**kw)

    def test_meta_dead_disabled(self):
        result = self._run(self._files(disabled=True))
        row = result["accounts"][0]
        self.assertTrue(row["in_cpa"])
        self.assertFalse(row["cpa_valid"])
        self.assertEqual(row["dead_by"], "meta")
        self.assertEqual(result["summary"]["cpa_dead"], 1)

    def test_meta_dead_status_error(self):
        result = self._run(self._files(status="error"))
        self.assertEqual(result["accounts"][0]["dead_by"], "meta")
        self.assertFalse(result["accounts"][0]["cpa_valid"])

    def test_meta_dead_status_unavailable(self):
        result = self._run(self._files(status="unavailable"))
        self.assertEqual(result["accounts"][0]["dead_by"], "meta")

    def test_meta_dead_high_failed_hits_threshold(self):
        result = self._run(self._files(failed=30), failed_threshold=20)
        self.assertEqual(result["accounts"][0]["dead_by"], "meta")

    def test_high_failed_below_threshold_probes_and_valid(self):
        result = self._run(self._files(failed=10), failed_threshold=20, probe_result=False)
        self.assertTrue(result["accounts"][0]["cpa_valid"])
        self.assertEqual(result["accounts"][0]["dead_by"], "")

    def test_probe_401_marks_dead(self):
        result = self._run(self._files(), probe_result=True)
        row = result["accounts"][0]
        self.assertFalse(row["cpa_valid"])
        self.assertEqual(row["dead_by"], "401")
        self.assertEqual(result["summary"]["cpa_dead"], 1)
        self.assertEqual(result["summary"]["cpa_valid"], 0)

    def test_probe_non_401_marks_valid(self):
        result = self._run(self._files(), probe_result=False)
        row = result["accounts"][0]
        self.assertTrue(row["cpa_valid"])
        self.assertEqual(row["dead_by"], "")
        self.assertEqual(result["summary"]["cpa_dead"], 0)
        self.assertEqual(result["summary"]["cpa_valid"], 1)

    def test_probe_skipped_when_probe_401_false(self):
        with patch("core.db.list_accounts", return_value=[{"email": "a@x.com", "codex_status": "success"}]), \
             patch("core.codex_oauth.list_cpa_codex_auth_files", return_value=self._files()), \
             patch("core.cpa_reauth._is_http_401", side_effect=AssertionError("不应探测")) as probe:
            result = align.align_account_pool_vs_cpa(probe_401=False)
        self.assertTrue(result["accounts"][0]["cpa_valid"])
        probe.assert_not_called()

    def test_probe_workers_clamped(self):
        with patch("core.db.list_accounts", return_value=[{"email": "a@x.com", "codex_status": "success"}] * 2), \
             patch("core.codex_oauth.list_cpa_codex_auth_files", return_value=[
                 {"name": f"codex-{e}-free.json", "email": e, "status": "active", "success": 1, "failed": 0}
                 for e in ("a@x.com", "b@x.com")
             ]), \
             patch("core.cpa_reauth._is_http_401", return_value=False):
            result = align.align_account_pool_vs_cpa(probe_workers=999)
        self.assertEqual(result["summary"]["cpa_valid"], 2)


class CpaPoolAlignCpaOnlyTests(unittest.TestCase):
    def test_cpa_only_excludes_matched_and_keeps_unmatched(self):
        pool = [
            {"email": "a@x.com", "codex_status": "success"},
            {"email": "b@x.com", "codex_status": "success"},
        ]
        files = [
            {"name": "codex-a@x.com-free.json", "email": "a@x.com", "status": "active", "success": 1, "failed": 0},
            {"name": "codex-b@x.com-free.json", "email": "b@x.com", "status": "active", "success": 1, "failed": 0},
            {"name": "codex-x@x.com-free.json", "email": "x@x.com", "status": "error"},
            {"name": "codex-y@x.com-free.json", "email": "y@x.com", "status": "disabled"},
        ]
        with patch("core.db.list_accounts", return_value=pool), \
             patch("core.codex_oauth.list_cpa_codex_auth_files", return_value=files), \
             patch("core.cpa_reauth._is_http_401", return_value=False):
            result = align.align_account_pool_vs_cpa()
        self.assertEqual([o["email"] for o in result["cpa_only"]], ["x@x.com", "y@x.com"])
        self.assertEqual(result["summary"]["cpa_only"], 2)
        self.assertEqual(result["summary"]["in_cpa"], 2)

    def test_cpa_only_duplicate_email_kept_once(self):
        pool = []
        files = [
            {"name": "codex-a@x.com-free.json", "email": "a@x.com", "status": "active"},
            {"name": "codex-a@x.com-free-2.json", "email": "a@x.com", "status": "active"},
        ]
        with patch("core.db.list_accounts", return_value=pool), \
             patch("core.codex_oauth.list_cpa_codex_auth_files", return_value=files):
            result = align.align_account_pool_vs_cpa(probe_401=False)
        # 池为空时没有匹配，两个文件都属仅 CPA
        self.assertEqual(len(result["cpa_only"]), 2)


if __name__ == "__main__":
    unittest.main()
