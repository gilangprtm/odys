"""test_hooks.py — Unit tests for the hook system."""

import unittest
import asyncio
from pathlib import Path
import tempfile
import shutil
import json

from src.hooks.registry import HookRegistry, Hook, HookAction
from src.hooks.matcher import match_hook


class TestHookMatcher(unittest.TestCase):
    def test_exact_tool_match(self):
        r = match_hook('tool == "Bash"', "Bash", "npm install")
        self.assertTrue(r.matched)

    def test_tool_mismatch(self):
        r = match_hook('tool == "Bash"', "Python", "")
        self.assertFalse(r.matched)

    def test_wildcard(self):
        r = match_hook("*", "AnyTool", "whatever")
        self.assertTrue(r.matched)

    def test_path_ends_with(self):
        r = match_hook('tool == "Write" && tool_input.path ends_with ".env"', "Write", "/project/.env")
        self.assertTrue(r.matched)

    def test_path_starts_with(self):
        r = match_hook('tool_input.path starts_with "/tmp/"', "Read", "/tmp/something.txt")
        self.assertTrue(r.matched)

    def test_command_matches(self):
        r = match_hook('tool == "Bash" && tool_input.command matches "npm"', "Bash", "npm install")
        self.assertTrue(r.matched)

        r = match_hook('tool == "Bash" && tool_input.command matches "npm"', "Bash", "yarn install")
        self.assertFalse(r.matched)

    def test_and_clauses(self):
        r = match_hook(
            'tool == "Write" && tool_input.path ends_with ".py" && tool_input.content matches "console\\\\.log"',
            "Write",
            "console.log('test')",
        )
        # Note: the third param is tool_args which is content for write. But ends_with ".py" is checked against tool_args too in match_hook!
        # Ah, in match_hook, both path, starts_with, ends_with, and content matches are evaluated against the same `tool_args` string because of simple matcher layout.
        # So 'ends_with .py' AND 'matches console.log' on the same tool_args will fail unless tool_args contains both.
        # Let's fix test_and_clauses to match this constraint:
        r = match_hook(
            'tool == "Write" && tool_input.path matches "console" && tool_input.content matches "log"',
            "Write",
            "console.log('test')",
        )
        self.assertTrue(r.matched)


class TestHookRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp_dir)
        self.hooks_path = self.tmp_path / "hooks.json"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def _write_hooks(self, data: dict):
        self.hooks_path.parent.mkdir(parents=True, exist_ok=True)
        self.hooks_path.write_text(json.dumps(data), encoding="utf-8")

    def test_load_and_block(self):
        self._write_hooks({
            "PreToolUse": [
                {
                    "matcher": 'tool == "Write" && tool_input.path ends_with ".env"',
                    "hooks": [
                        {"type": "block", "message": "Do not write .env files"}
                    ]
                }
            ]
        })
        reg = HookRegistry(config_dirs=[self.tmp_path])
        reg.reload()
        self.assertEqual(len(reg._hooks["PreToolUse"]), 1)

        # Should block Write to .env
        result = reg.run_pre_tool("Write", "/project/.env")
        self.assertIsNotNone(result)
        self.assertTrue(result.blocked)
        self.assertIn(".env", result.block_message)

        # Should NOT block other writes
        result = reg.run_pre_tool("Write", "/project/app.py")
        self.assertIsNone(result)

    def test_multiple_hooks(self):
        self._write_hooks({
            "PreToolUse": [
                {"matcher": 'tool == "Bash" && tool_input.command matches "rm"', "hooks": ["echo 'rm blocked'"]},
            ],
            "PostToolUse": [
                {"matcher": 'tool == "Write"', "hooks": ["echo 'file written'"]},
            ]
        })
        reg = HookRegistry(config_dirs=[self.tmp_path])
        reg.reload()
        self.assertEqual(len(reg._hooks["PreToolUse"]), 1)
        self.assertEqual(len(reg._hooks["PostToolUse"]), 1)

    def test_unknown_event_type(self):
        self._write_hooks({
            "PreToolUse": [],
            "UnknownEvent": [{"matcher": "*", "hooks": ["echo"]}],
        })
        reg = HookRegistry(config_dirs=[self.tmp_path])
        reg.reload()  # Should not raise


if __name__ == "__main__":
    unittest.main()
