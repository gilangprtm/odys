"""test_rules.py — Unit tests for the rules engine."""

import unittest
from pathlib import Path
import tempfile
import shutil

from src.rules.engine import RulesEngine


class TestRulesEngine(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_load_rules(self):
        # Create mock rules
        sec_rule = self.tmp_path / "security.md"
        sec_rule.write_text("# Security\n\nNever hardcode secrets.", encoding="utf-8")

        style_rule = self.tmp_path / "coding-style.md"
        style_rule.write_text("# Style\n\nUse immutability.", encoding="utf-8")

        gen_rule = self.tmp_path / "general.md"
        gen_rule.write_text("# General\n\nBe concise.", encoding="utf-8")

        eng = RulesEngine(search_dirs=[self.tmp_path])
        n = eng.reload()

        self.assertEqual(n, 3)
        self.assertIn("security", eng.rules)
        self.assertIn("coding-style", eng.rules)
        self.assertIn("general", eng.rules)

    def test_render_output(self):
        sec_rule = self.tmp_path / "security.md"
        sec_rule.write_text("Never hardcode secrets.", encoding="utf-8")

        style_rule = self.tmp_path / "coding-style.md"
        style_rule.write_text("Use immutability.", encoding="utf-8")

        eng = RulesEngine(search_dirs=[self.tmp_path], max_tokens=2000)
        eng.reload()

        output = eng.render()
        self.assertIn("## Persistent Rules", output)
        self.assertIn("### security", output)
        self.assertIn("Never hardcode secrets.", output)
        self.assertIn("### coding-style", output)
        self.assertIn("Use immutability.", output)
        # security (priority 0) comes before coding-style (priority 1)
        self.assertTrue(output.index("### security") < output.index("### coding-style"))

    def test_project_overrides_user(self):
        # Simulate user rule then project rule with same name
        sec_user = self.tmp_path / "user-rules" / "security.md"
        sec_user.parent.mkdir()
        sec_user.write_text("Old rule.", encoding="utf-8")

        sec_proj = self.tmp_path / "project-rules" / "security.md"
        sec_proj.parent.mkdir()
        sec_proj.write_text("New rule.", encoding="utf-8")

        eng = RulesEngine(search_dirs=[
            self.tmp_path / "user-rules",
            self.tmp_path / "project-rules",
        ])
        eng.reload()
        self.assertEqual(len(eng.rules), 1)
        self.assertIn("New rule.", eng.rules["security"].content)
        self.assertNotIn("Old rule.", eng.rules["security"].content)

    def test_token_budget_enforcement(self):
        # Create many large rules
        for i in range(10):
            rule_file = self.tmp_path / f"rule-{i}.md"
            rule_file.write_text(f"# Rule {i}\n\n" + "word " * 2000, encoding="utf-8")

        eng = RulesEngine(search_dirs=[self.tmp_path], max_tokens=200)
        eng.reload()
        output = eng.render()

        # Should be truncated due to budget
        self.assertIn("## Persistent Rules", output)
        # Should NOT contain all 10 rules
        count = output.count("### rule-")
        self.assertLess(count, 10)

    def test_reject_secrets(self):
        rule = self.tmp_path / "bad-rule.md"
        rule.write_text("# Bad\n\nAPI key: sk-1234567890abcdefghij", encoding="utf-8")

        eng = RulesEngine(search_dirs=[self.tmp_path])
        n = eng.reload()
        self.assertEqual(n, 0)  # Rejected

    def test_empty_rules_dir(self):
        eng = RulesEngine(search_dirs=[self.tmp_path])
        n = eng.reload()
        self.assertEqual(n, 0)
        self.assertEqual(eng.render(), "")


if __name__ == "__main__":
    unittest.main()
