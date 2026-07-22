"""test_skills_advanced.py — Advanced tests for version pinning, hot reload, and skill composition."""

import asyncio
import unittest
from pathlib import Path
import tempfile
import shutil
import time

from src.skills.registry import SkillRegistry
from src.skills.loader import parse_skill_md


class TestSkillsAdvanced(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_version_pinning_compatible(self):
        text = """---
name: compatible-skill
description: Comp skill
origin: ecc@^2.0
---
Body"""
        skill = parse_skill_md(text)
        self.assertIsNotNone(skill)
        self.assertTrue(skill.version_ok)

    def test_version_pinning_incompatible(self):
        text = """---
name: incompatible-skill
description: Incomp skill
origin: ecc@^3.0
---
Body"""
        skill = parse_skill_md(text)
        self.assertIsNotNone(skill)
        self.assertFalse(skill.version_ok)
        
        # Incompatible version warning is appended to description in tool schema
        schema = skill.to_tool_schema()
        self.assertIn("WARN: version mismatch", schema["function"]["description"])

    def test_composition(self):
        # Create dependent skills
        b_md = self.tmp_path / "skill-b.md"
        b_md.write_text("""---
name: skill-b
description: Dependency
origin: ecc@^2.0
---
Content B""", encoding="utf-8")

        a_md = self.tmp_path / "skill-a.md"
        a_md.write_text("""---
name: skill-a
description: Root skill
origin: ecc@^2.0
requires: [skill-b]
---
Content A""", encoding="utf-8")

        reg = SkillRegistry(search_dirs=[self.tmp_path])
        reg.reload()

        res = asyncio.run(reg.execute_skill("skill_skill_a", "Go"))
        output = res.get("output", "")
        
        self.assertIn("## Required Skill: skill-b", output)
        self.assertIn("Content B", output)
        self.assertIn("Content A", output)

    def test_hot_reload(self):
        skill_md = self.tmp_path / "dynamic-skill.md"
        skill_md.write_text("""---
name: dynamic
description: V1
origin: ecc@^2.0
---
Body V1""", encoding="utf-8")

        reg = SkillRegistry(search_dirs=[self.tmp_path])
        reg.reload()

        self.assertEqual(reg.get_skill_by_name("dynamic").description, "V1")

        # Sleep briefly to ensure mtime changes
        time.sleep(0.1)

        # Update file
        skill_md.write_text("""---
name: dynamic
description: V2
origin: ecc@^2.0
---
Body V2""", encoding="utf-8")

        # Check hot reload
        n = reg.hot_reload_if_changed()
        self.assertGreater(n, 0)
        self.assertEqual(reg.get_skill_by_name("dynamic").description, "V2")


if __name__ == "__main__":
    unittest.main()
