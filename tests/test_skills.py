"""test_skills.py — Unit tests for the ECC skill system integration."""

import unittest
from pathlib import Path
import tempfile
import shutil
import asyncio

from src.skills.models import Skill
from src.skills.loader import parse_skill_md, discover_skills
from src.skills.registry import SkillRegistry


class TestSkillSystem(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_parse_skill_md(self):
        text = """---
name: test-skill
description: A mock skill for testing
origin: ecc@^2.0
tools: [bash, python]
when: "User triggers test-skill"
---
# Test Skill Markdown Body
Instruction details go here.
"""
        skill = parse_skill_md(text, path="mock_skill.md")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "test-skill")
        self.assertEqual(skill.description, "A mock skill for testing")
        self.assertEqual(skill.origin, "ecc@^2.0")
        self.assertEqual(skill.tools, ["bash", "python"])
        self.assertEqual(skill.when, "User triggers test-skill")
        self.assertIn("Instruction details go here.", skill.content)

    def test_discover_skills(self):
        # Setup mock skill files
        skill_dir = self.tmp_path / "my-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: tdd-skill
description: TDD style
tools: [bash]
---
TDD instructions
""", encoding="utf-8")

        flat_skill = self.tmp_path / "flat-skill.md"
        flat_skill.write_text("""---
name: flat-skill
description: Flat style
tools: [python]
---
Flat instructions
""", encoding="utf-8")

        skills = discover_skills([self.tmp_path])
        self.assertEqual(len(skills), 2)
        names = {s.name for s in skills}
        self.assertIn("tdd-skill", names)
        self.assertIn("flat-skill", names)

    def test_registry_tool_schema(self):
        reg = SkillRegistry(search_dirs=[self.tmp_path])
        
        # Write mock skill
        skill_dir = self.tmp_path / "mock-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: test-action
description: Executes test action
tools: [python]
when: "User asks for action"
---
Instructions here
""", encoding="utf-8")

        reg.reload()
        self.assertIn("skill_test_action", reg.tool_names)
        
        schemas = reg.get_all_schemas()
        self.assertEqual(len(schemas), 1)
        self.assertEqual(schemas[0]["function"]["name"], "skill_test_action")
        self.assertIn("[SKILL]", schemas[0]["function"]["description"])

        # Test execution V1 payload
        loop = asyncio.get_event_loop()
        res = loop.run_until_complete(reg.execute_skill("skill_test_action", "Run fast"))
        self.assertTrue(res.get("skill"))
        self.assertEqual(res.get("skill_name"), "test-action")
        self.assertIn("Instructions here", res.get("output"))
        self.assertIn("**User instruction:** Run fast", res.get("output"))


if __name__ == "__main__":
    unittest.main()
