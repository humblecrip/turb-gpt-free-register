# -*- coding: utf-8 -*-
"""回归测试：状态/来源自动分类标签批量匹配（match_account_ids_by_tags）。

覆盖 db.ACCOUNT_TAG_KEYS 每个标签的规则、OR 并集语义、未归档作用域与未知标签忽略。
"""
import unittest
from unittest import mock

from core import db


def _row(acc_id, email, **overrides):
    row = {
        "id": acc_id,
        "email": email,
        "email_source": "",
        "access_token": "tok",
        "plan_type": None,
        "current_plan_type": None,
        "live_check_status": None,
        "codex_status": None,
        "token_expired": False,
        "archived": False,
    }
    row.update(overrides)
    return row


class MatchAccountIdsByTagsTests(unittest.TestCase):
    def _match(self, tags, rows):
        with mock.patch.object(db, "_load_accounts", return_value=rows):
            return db.match_account_ids_by_tags(tags)

    def test_dead_matches_deactivated_only(self):
        rows = [
            _row(1, "a@outlook.com", live_check_status="deactivated"),
            _row(2, "b@outlook.com", live_check_status="live"),
            _row(3, "c@outlook.com", live_check_status="failed"),
        ]
        self.assertEqual(self._match(["dead"], rows), [1])

    def test_live_failed(self):
        rows = [
            _row(1, "a@outlook.com", live_check_status="failed"),
            _row(2, "b@outlook.com", live_check_status="deactivated"),
        ]
        self.assertEqual(self._match(["live_failed"], rows), [1])

    def test_live_pending_accepts_empty_and_dash(self):
        rows = [
            _row(1, "a@outlook.com", live_check_status=""),
            _row(2, "b@outlook.com", live_check_status="-"),
            _row(3, "c@outlook.com", live_check_status="live"),
        ]
        self.assertEqual(self._match(["live_pending"], rows), [1, 2])

    def test_outlook_by_email_source(self):
        rows = [
            _row(1, "a@example.com", email_source="outlook"),
            _row(2, "b@example.com", email_source="icloud_hme"),
        ]
        self.assertEqual(self._match(["outlook"], rows), [1])

    def test_outlook_by_domain_fallback(self):
        rows = [
            _row(1, "a@outlook.com"),
            _row(2, "b@hotmail.com"),
            _row(3, "c@live.com"),
            _row(4, "d@icloud.com"),
            _row(5, "e@example.com"),
        ]
        self.assertEqual(self._match(["outlook"], rows), [1, 2, 3])

    def test_icloud_by_email_source(self):
        rows = [
            _row(1, "a@example.com", email_source="icloud_hme"),
            _row(2, "b@example.com", email_source="outlook"),
        ]
        self.assertEqual(self._match(["icloud"], rows), [1])

    def test_icloud_by_domain_fallback(self):
        rows = [
            _row(1, "a@icloud.com"),
            _row(2, "b@me.com"),
            _row(3, "c@mac.com"),
            _row(4, "d@outlook.com"),
            _row(5, "e@example.com"),
        ]
        self.assertEqual(self._match(["icloud"], rows), [1, 2, 3])

    def test_no_token(self):
        rows = [
            _row(1, "a@outlook.com", access_token=""),
            _row(2, "b@outlook.com", access_token="  "),
            _row(3, "c@outlook.com", access_token="tok"),
        ]
        self.assertEqual(self._match(["no_token"], rows), [1, 2])

    def test_plus_plan(self):
        rows = [
            _row(1, "a@outlook.com", plan_type="plus"),
            _row(2, "b@outlook.com", current_plan_type="chatgpt_plus"),
            _row(3, "c@outlook.com", plan_type="free", plus_trial_eligible=True),
            _row(4, "d@outlook.com", current_plan_type="free"),
            _row(5, "e@outlook.com", plan_type="pro"),
        ]
        self.assertEqual(self._match(["plus"], rows), [1, 2])

    def test_free_plan(self):
        rows = [
            _row(1, "a@outlook.com", current_plan_type="free"),
            _row(2, "b@outlook.com", plan_type="free"),
            _row(3, "c@outlook.com", plan_type="plus"),
        ]
        self.assertEqual(self._match(["free"], rows), [1, 2])

    def test_token_expired(self):
        rows = [
            _row(1, "a@outlook.com", token_expired=True),
            _row(2, "b@outlook.com", token_expired=False),
            _row(3, "c@outlook.com", token_expired=None),
        ]
        self.assertEqual(self._match(["token_expired"], rows), [1])

    def test_codex_success_and_failed(self):
        rows = [
            _row(1, "a@outlook.com", codex_status="success"),
            _row(2, "b@outlook.com", codex_status="failed"),
            _row(3, "c@outlook.com", codex_status="deactivated"),
        ]
        self.assertEqual(self._match(["codex_success"], rows), [1])
        self.assertEqual(self._match(["codex_failed"], rows), [2])

    def test_codex_pending(self):
        rows = [
            _row(1, "a@outlook.com", codex_status="success"),
            _row(2, "b@outlook.com", codex_status="failed"),
            _row(3, "c@outlook.com", codex_status="deactivated"),
            _row(4, "d@outlook.com", codex_status="stopped"),
            _row(5, "e@outlook.com", codex_status="retrying"),
            _row(6, "f@outlook.com", codex_status=""),
            _row(7, "g@outlook.com", codex_status=None),
        ]
        self.assertEqual(self._match(["codex_pending"], rows), [4, 5, 6, 7])

    def test_or_semantics_across_tags(self):
        rows = [
            _row(1, "a@outlook.com", live_check_status="deactivated"),
            _row(2, "b@outlook.com", codex_status="success"),
            _row(3, "c@icloud.com"),
            _row(4, "d@outlook.com", plan_type="plus"),
        ]
        # dead + codex_success + icloud => ids 1,2,3（不重复，不含 4）
        self.assertEqual(self._match(["dead", "codex_success", "icloud"], rows), [1, 2, 3])

    def test_archived_accounts_excluded(self):
        rows = [
            _row(1, "a@outlook.com", live_check_status="deactivated"),
            _row(2, "b@outlook.com", live_check_status="deactivated", archived=True),
        ]
        self.assertEqual(self._match(["dead"], rows), [1])

    def test_unknown_tags_ignored(self):
        rows = [
            _row(1, "a@outlook.com", live_check_status="deactivated"),
            _row(2, "b@outlook.com", codex_status="success"),
        ]
        self.assertEqual(self._match(["unknown_tag", "dead"], rows), [1])
        self.assertEqual(self._match(["unknown_tag"], rows), [])
        self.assertEqual(self._match([], rows), [])
        self.assertEqual(self._match(None, rows), [])

    def test_domain_fallback_respects_explicit_email_source(self):
        # email_source 非空时优先使用字段，域名兜底不覆盖
        rows = [
            _row(1, "a@icloud.com", email_source="outlook"),
            _row(2, "b@outlook.com", email_source="icloud_hme"),
        ]
        self.assertEqual(self._match(["outlook"], rows), [1])
        self.assertEqual(self._match(["icloud"], rows), [2])


if __name__ == "__main__":
    unittest.main()
