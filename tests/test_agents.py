"""test_agents.py — Unit tests for the agent definitions registry."""

import unittest
from pathlib import Path
import tempfile
import shutil

from src.agent_tools.agent_definitions import AgentRegistry


class TestAgentRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_load_agent_definitions(self):
        # Create a mock agent markdown file
        planner = self.tmp_path / "planner.md"
        planner.write_text("System instructions for planner.", encoding="utf-8")

        reviewer = self.tmp_path / "code-reviewer.md"
        reviewer.write_text("System instructions for code reviewer.", encoding="utf-8")

        reg = AgentRegistry(search_dirs=[self.tmp_path])
        n = reg.reload()

        self.assertEqual(n, 2)
        self.assertIn("planner", reg.agents)
        self.assertIn("code-reviewer", reg.agents)

        agent = reg.get_agent("planner")
        self.assertIsNotNone(agent)
        self.assertEqual(agent.instructions, "System instructions for planner.")

    def test_slug_resolution(self):
        reviewer = self.tmp_path / "code-reviewer.md"
        reviewer.write_text("System instructions.", encoding="utf-8")

        reg = AgentRegistry(search_dirs=[self.tmp_path])
        reg.reload()

        # Both code-reviewer and code_reviewer should resolve
        a1 = reg.get_agent("code-reviewer")
        a2 = reg.get_agent("code_reviewer")
        self.assertIsNotNone(a1)
        self.assertIsNotNone(a2)
        self.assertEqual(a1.path, a2.path)


if __name__ == "__main__":
    unittest.main()
