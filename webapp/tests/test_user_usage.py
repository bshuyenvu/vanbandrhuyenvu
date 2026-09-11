from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from webapp.saas import store
from webapp.saas.security import new_id
from webapp.saas.usage import ensure_usage_quota, usage_summary


class UserUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "usage.db"
        store.init_db()
        self.wallet = store.create_personal_wallet("usr_usage", "free")

    def tearDown(self):
        store.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _usage(self, input_tokens: int, output_tokens: int) -> None:
        store.execute(
            """INSERT INTO ai_usage(id,user_id,wallet_id,model_id,task_type,input_tokens,cached_tokens,output_tokens,provider_cost_usd,charged_credits)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (new_id("use_"), "usr_usage", self.wallet["id"], "gpt-luna", "draft", input_tokens, 0, output_tokens, 0, 1),
        )

    def test_summary_reports_used_and_remaining(self):
        self._usage(1000, 500)
        self._usage(2000, 1000)
        summary = usage_summary(user_id="usr_usage", plan_id="free", wallet=self.wallet)
        self.assertEqual(summary["quota"]["requests_used"], 2)
        self.assertEqual(summary["quota"]["requests_remaining"], 28)
        self.assertEqual(summary["quota"]["tokens_used"], 4500)
        self.assertEqual(summary["quota"]["tokens_remaining"], 195500)
        self.assertEqual(summary["month"]["input_tokens"], 3000)
        self.assertEqual(summary["month"]["output_tokens"], 1500)

    def test_request_quota_blocks_next_request(self):
        for _ in range(30):
            self._usage(1, 1)
        with self.assertRaisesRegex(ValueError, "hết lượt AI"):
            ensure_usage_quota(user_id="usr_usage", plan_id="free", wallet=self.wallet)

    def test_token_quota_blocks_next_request(self):
        self._usage(150000, 50000)
        with self.assertRaisesRegex(ValueError, "hết quota token"):
            ensure_usage_quota(user_id="usr_usage", plan_id="free", wallet=self.wallet)


if __name__ == "__main__":
    unittest.main()
