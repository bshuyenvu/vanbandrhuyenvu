from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from webapp.saas import store
from webapp.saas.model_manager import choose_model


class BudgetGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "budget.db"
        store.init_db()

    def tearDown(self):
        store.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_router_skips_model_after_daily_budget(self):
        store.execute("UPDATE ai_models SET daily_budget_usd=? WHERE id=?", (0.001, "gpt-luna"))
        store.execute(
            "INSERT INTO ai_usage(id,model_id,task_type,provider_cost_usd) VALUES(?,?,?,?)",
            ("use_budget", "gpt-luna", "draft", 0.01),
        )
        rows = store.all_rows("SELECT * FROM ai_models WHERE enabled=1 ORDER BY id")
        registry = {row["id"]: {**row, "enabled": bool(row["enabled"])} for row in rows}
        choice = choose_model(task_type="draft", plan_id="personal", registry=registry)
        self.assertEqual(choice.model_id, "gemini-flash")


if __name__ == "__main__":
    unittest.main()
